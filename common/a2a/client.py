"""Production A2A HTTP Client

A full-featured HTTP client for interacting with remote A2A agents.

Usage — quick start:
    from common.a2a.client import A2AClient

    client = A2AClient("https://dev-nexus-abc123-uc.a.run.app", auth_token="ghp_...")
    client.discover()                      # Print available skills
    result = client.execute("get_dashboard_overview", {})  # Sync call
    wf = client.execute("build_new_project", {...})        # Async returns workflow_id
    final = client.poll_workflow(wf["workflow_id"])        # Block until done

Usage — context manager:
    with A2AClient(base_url, auth_token) as c:
        c.execute(...)
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

from common.a2a.exceptions import (
    A2AError,
    A2AConnectionError,
    A2AAuthenticationError,
    A2ASkillNotFoundError,
    A2ASkillExecutionError,
    A2AWorkflowTimeoutError,
    A2AWorkflowNotFoundError,
    A2AValidationError,
)

logger = logging.getLogger("common.a2a.client")

# ---------------------------------------------------------------------------
# Retry policy defaults
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 30          # seconds for single request
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0    # seconds
DEFAULT_BACKOFF_MAX = 30.0    # seconds

# Terminal workflow states — polling stops here
TERMINAL_STATES = {"completed", "failed", "partial_success"}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    """Parsed AgentCard from /.well-known/agent.json"""
    name: str
    description: str
    version: str
    url: str
    capabilities: Dict[str, Any] = field(default_factory=dict)
    skill_count: int = 0
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentInfo":
        return cls(
            name=data.get("name", "unknown"),
            description=data.get("description", ""),
            version=data.get("version", "0.0.0"),
            url=data.get("url", ""),
            capabilities=data.get("capabilities", {}),
            skill_count=data.get("metadata", {}).get("skill_count", 0),
            raw=data,
        )


@dataclass
class SkillInfo:
    """Parsed skill entry from the A2A manifest."""
    skill_id: str
    skill_name: str
    description: str
    tags: List[str] = field(default_factory=list)
    authentication_required: bool = False
    input_schema: Dict[str, Any] = field(default_factory=dict)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "stable"
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillInfo":
        return cls(
            skill_id=data["skill_id"],
            skill_name=data.get("skill_name", data["skill_id"]),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            authentication_required=data.get("authentication_required", False),
            input_schema=data.get("input", {}),
            examples=data.get("examples", []),
            status=data.get("status", "stable"),
            raw=data,
        )

    def example_input(self) -> Optional[Dict[str, Any]]:
        """Return the first example input, or None."""
        if self.examples:
            return self.examples[0].get("input")
        return None


# ---------------------------------------------------------------------------
# Core client
# ---------------------------------------------------------------------------

class A2AClient:
    """
    Production HTTP client for A2A protocol servers.

    All public methods are **synchronous** (blocking). This keeps the API
    simple for CLI scripts and REPL use. Async wrappers can be added
    trivially with ``asyncio.to_thread`` if needed.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
    ):
        """
        Args:
            base_url: Root URL of the A2A server.
            auth_token: Bearer token — GitHub OAuth JWT or static A2A token.
            timeout: Request timeout in seconds.
            max_retries: How many times to retry on transient failures.
            backoff_base: Exponential backoff base in seconds.
            backoff_max: Cap for backoff sleep.
        """
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token or os.environ.get("A2A_TOKEN", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

        # Cached manifest / agent card
        self._agent_info: Optional[AgentInfo] = None
        self._skills: Dict[str, SkillInfo] = {}
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"
        if self.auth_token:
            self._session.headers["Authorization"] = f"Bearer {self.auth_token}"

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "A2AClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    # ------------------------------------------------------------------
    # Internal request helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Dict[str, Any] | None = None,
        extra_headers: Dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> Dict[str, Any]:
        """
        Make an HTTP request with retry + backoff on transient failures.
        Returns parsed JSON. Raises structured exceptions on hard failures.
        """
        url = self._url(path)
        headers = dict(self._session.headers)
        if extra_headers:
            headers.update(extra_headers)
        to = timeout or self.timeout

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    json=json_payload,
                    headers=headers,
                    timeout=to,
                )
                # Handle HTTP status codes
                if resp.status_code == 401:
                    raise A2AAuthenticationError(
                        f"Authentication failed for {url}. Token invalid or missing.",
                        details={"url": url, "status": resp.status_code},
                    )
                if resp.status_code == 404:
                    body = resp.text
                    # Distinguish workflow-not-found from skill-not-found
                    if "workflow" in body.lower() or "workflow" in path.lower():
                        raise A2AWorkflowNotFoundError(
                            f"Workflow not found at {url}",
                            workflow_id=path.split("/")[-1] if "/" in path else None,
                        )
                    raise A2ASkillNotFoundError(
                        f"Skill or endpoint not found: {url}",
                        details={"url": url, "response": body[:500]},
                    )
                if resp.status_code == 400:
                    raise A2AValidationError(
                        f"Bad request to {url}: {resp.text[:500]}",
                        details={"url": url, "response": resp.text[:500]},
                    )
                resp.raise_for_status()
                return resp.json()

            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                if attempt < self.max_retries:
                    sleep = min(
                        self.backoff_base * (2 ** attempt),
                        self.backoff_max,
                    )
                    logger.warning(
                        "[A2A] Transient error on %s (attempt %d/%d), retrying in %.1fs: %s",
                        url, attempt + 1, self.max_retries + 1, sleep, e,
                    )
                    time.sleep(sleep)
                continue

            except (
                A2AAuthenticationError,
                A2ASkillNotFoundError,
                A2AValidationError,
                A2AWorkflowNotFoundError,
            ):
                raise  # Non-retryable

            except requests.HTTPError as e:
                # 5xx = retryable; everything else is a hard failure
                status_code = getattr(e.response, "status_code", 0) if e.response else 0
                if 500 <= status_code < 600 and attempt < self.max_retries:
                    last_err = e
                    sleep = min(self.backoff_base * (2 ** attempt), self.backoff_max)
                    logger.warning(
                        "[A2A] Server error %d on %s (attempt %d/%d), retrying in %.1fs",
                        status_code, url, attempt + 1, self.max_retries + 1, sleep,
                    )
                    time.sleep(sleep)
                    continue
                raise A2AError(
                    f"HTTP {e.response.status_code} from {url}: {e.response.text[:500]}",
                    details={"url": url, "status": e.response.status_code, "body": e.response.text[:500]},
                ) from e

        # Exhausted retries
        raise A2AConnectionError(
            f"Failed to reach {url} after {self.max_retries + 1} attempts: {last_err}",
            details={"url": url, "last_error": str(last_err)},
        ) from last_err

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Return True if the server responds with HTTP 200 on /health."""
        try:
            self._request("GET", "/health", timeout=5)
            return True
        except A2AConnectionError:
            return False
        except A2AError:
            return False

    def get_agent_card(self) -> AgentInfo:
        """Fetch and cache the AgentCard from /.well-known/agent.json."""
        data = self._request("GET", "/.well-known/agent.json")
        self._agent_info = AgentInfo.from_dict(data)
        return self._agent_info

    def get_manifest(self) -> Dict[str, Any]:
        """Fetch the full A2A skills manifest from /.well-known/a2a-manifest.json."""
        return self._request("GET", "/.well-known/a2a-manifest.json")

    def list_skills(self, refresh: bool = False) -> List[SkillInfo]:
        """
        Return a list of all skills advertised by the server.
        Caches after first call unless *refresh=True*.
        """
        if not self._skills or refresh:
            manifest = self.get_manifest()
            raw_skills = manifest.get("skills", [])
            self._skills = {
                s["skill_id"]: SkillInfo.from_dict(s)
                for s in raw_skills
                if "skill_id" in s
            }
        return list(self._skills.values())

    def get_skill(self, skill_id: str, refresh: bool = False) -> SkillInfo:
        """Return metadata for a single skill. Raises A2ASkillNotFoundError if absent."""
        if refresh or skill_id not in self._skills:
            self.list_skills(refresh=True)
        if skill_id not in self._skills:
            raise A2ASkillNotFoundError(
                f"Skill '{skill_id}' not found on server {self.base_url}.",
                details={"skill_id": skill_id},
            )
        return self._skills[skill_id]

    def discover(self) -> None:
        """
        Print a human-readable skill catalogue to stdout.
        Useful for REPL exploration and CLI bootstrap.
        """
        try:
            agent = self.get_agent_card()
        except A2AError as e:
            print(f"Cannot reach agent at {self.base_url}: {e}")
            return

        print(f"\n{agent.name}  v{agent.version}")
        print(f"   {agent.description}")
        print(f"   URL: {self.base_url}")
        print(f"   Skills: {agent.skill_count}")
        print()

        skills = self.list_skills()
        auth_skills = [s for s in skills if s.authentication_required]
        public_skills = [s for s in skills if not s.authentication_required]

        if public_skills:
            print(f"Public skills ({len(public_skills)}):")
            for s in public_skills:
                tags = ", ".join(s.tags) if s.tags else "—"
                print(f"   • {s.skill_id:35s}  {s.skill_name}")
                print(f"     tags: {tags}")
                if s.examples:
                    ex = s.examples[0]
                    inp = json.dumps(ex.get("input", {}), indent=6)[:200]
                    print(f"     example: {ex.get('description', '')}")
                    print(f"       input: {inp}")
        if auth_skills:
            print(f"\nAuthenticated skills ({len(auth_skills)}):")
            for s in auth_skills:
                print(f"   • {s.skill_id:35s}  {s.skill_name}")

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        skill_id: str,
        input_data: Dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> Dict[str, Any]:
        """
        Execute a skill synchronously.

        For **quick** skills that return immediately.
        For long-running workflows, this returns a ``workflow_id`` — poll with
        :meth:`poll_workflow`.

        Args:
            skill_id: The skill identifier.
            input_data: Skill-specific input dictionary.
            timeout: Override the client default timeout.

        Returns:
            The skill's JSON response.

        Raises:
            A2ASkillExecutionError: If the skill returns ``success=False``.
            A2ASkillNotFoundError: If the skill does not exist.
            A2AAuthenticationError: If auth is required but missing/invalid.
        """
        payload = {"skill_id": skill_id, "input": input_data or {}}
        result = self._request("POST", "/a2a/execute", json_payload=payload, timeout=timeout)

        if result.get("success") is False:
            raise A2ASkillExecutionError(
                result.get("error", f"Skill '{skill_id}' returned success=False"),
                response=result,
            )
        return result

    def cancel_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Cancel a running workflow."""
        return self._request(
            "POST", "/a2a/cancel",
            json_payload={"workflow_id": workflow_id},
        )

    # ------------------------------------------------------------------
    # Workflow polling
    # ------------------------------------------------------------------

    def get_workflow_status(self, workflow_id: str) -> Dict[str, Any]:
        """Fetch the current status of a workflow (one-shot, no polling)."""
        return self._request("GET", f"/a2a/workflow/{workflow_id}")

    def poll_workflow(
        self,
        workflow_id: str,
        *,
        interval: int = 5,
        timeout: int = 300,
        on_progress: Callable[[Dict[str, Any]], None] | None = None,
    ) -> Dict[str, Any]:
        """
        Poll a workflow until it reaches a terminal state.

        Args:
            workflow_id: The workflow ID returned by an async skill.
            interval: Seconds between polls.
            timeout: Maximum total seconds to wait.
            on_progress: Optional callback(status_dict) called on every poll.

        Returns:
            The final workflow status dict.

        Raises:
            A2AWorkflowTimeoutError: If *timeout* is exceeded.
            A2AWorkflowNotFoundError: If the workflow never existed.
        """
        start = time.monotonic()
        last_status = "unknown"

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise A2AWorkflowTimeoutError(
                    f"Workflow {workflow_id} did not finish within {timeout}s "
                    f"(last status: {last_status}).",
                    last_status=last_status,
                )

            status = self.get_workflow_status(workflow_id)
            last_status = status.get("status", "unknown")

            if on_progress:
                on_progress(status)

            if last_status in TERMINAL_STATES:
                logger.info(
                    "[A2A] Workflow %s finished with status '%s' (%.1fs)",
                    workflow_id, last_status, elapsed,
                )
                return status

            logger.debug(
                "[A2A] Workflow %s status='%s', polling again in %ds",
                workflow_id, last_status, interval,
            )
            time.sleep(interval)

    def execute_and_poll(
        self,
        skill_id: str,
        input_data: Dict[str, Any] | None = None,
        *,
        poll_interval: int = 5,
        poll_timeout: int = 300,
        on_progress: Callable[[Dict[str, Any]], None] | None = None,
        execute_timeout: int | None = None,
    ) -> Dict[str, Any]:
        """
        Execute a skill and, if it returns a *workflow_id*, poll to completion.
        Returns either the direct skill result or the final workflow status.

        This is the **most convenient** method for local agents — it handles
        both sync and async skill patterns transparently.
        """
        result = self.execute(skill_id, input_data, timeout=execute_timeout)
        wf_id = result.get("workflow_id")
        if wf_id:
            final = self.poll_workflow(
                wf_id,
                interval=poll_interval,
                timeout=poll_timeout,
                on_progress=on_progress,
            )
            return final
        return result

    # ------------------------------------------------------------------
    # Skill-specific convenience wrappers (optional)
    # ------------------------------------------------------------------

    def get_dashboard_overview(self, time_range_days: int = 30) -> Dict[str, Any]:
        """Convenience wrapper for the ``get_dashboard_overview`` skill."""
        return self.execute("get_dashboard_overview", {"time_range_days": time_range_days})

    def assess_cicd_pipeline(
        self,
        repo_path: str,
        dimensions: List[str] | None = None,
        standards: str = "dev-nexus",
    ) -> Dict[str, Any]:
        """Convenience wrapper for the ``assess_cicd_pipeline`` skill."""
        return self.execute(
            "assess_cicd_pipeline",
            {
                "repo_path": repo_path,
                "dimensions": dimensions or ["precommit", "ci_workflows", "terraform_ci", "caching"],
                "standards": standards,
            },
        )


# ---------------------------------------------------------------------------
# External agent registry
# ---------------------------------------------------------------------------

class ExternalAgentRegistry:
    """
    Registry for managing connections to external A2A agents.
    Populated from environment variables on instantiation.
    """

    def __init__(self):
        self.agents: Dict[str, A2AClient] = {}
        self._logger = logging.getLogger("common.a2a.external_agent_registry")
        self._discover()

    def _discover(self) -> None:
        self._logger.info("[AGENT_DISCOVERY] Starting external agent discovery...")

        for name, url_env, token_env in [
            ("dependency-orchestrator", "ORCHESTRATOR_URL", "ORCHESTRATOR_TOKEN"),
            ("pattern-miner", "PATTERN_MINER_URL", "PATTERN_MINER_TOKEN"),
        ]:
            url = os.environ.get(url_env)
            if url:
                self.agents[name] = A2AClient(
                    base_url=url,
                    auth_token=os.environ.get(token_env),
                )
                self._logger.info("[AGENT_DISCOVERY] Registered '%s' -> %s", name, url)
            else:
                self._logger.debug("[AGENT_DISCOVERY] %s not set", url_env)

        if not self.agents:
            self._logger.warning("[AGENT_DISCOVERY] No external agents configured")

    def get_agent(self, agent_name: str) -> Optional[A2AClient]:
        return self.agents.get(agent_name)

    def list_agents(self) -> List[str]:
        return list(self.agents.keys())

    def health_check_all(self) -> Dict[str, bool]:
        return {name: client.health_check() for name, client in self.agents.items()}


# ---------------------------------------------------------------------------
# Quick CLI smoke-test
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="A2A client smoke test")
    parser.add_argument("--url", required=True, help="A2A server base URL")
    parser.add_argument("--token", default=os.environ.get("A2A_TOKEN", ""), help="Auth token")
    parser.add_argument("--discover", action="store_true", help="List all skills")
    parser.add_argument("--skill", help="Skill ID to execute")
    parser.add_argument("--input", default="{}", help="JSON input for the skill")
    parser.add_argument("--poll", action="store_true", help="Poll if workflow_id returned")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = A2AClient(args.url, auth_token=args.token or None)

    if not client.health_check():
        print(f"Server at {args.url} is not healthy")
        return 1

    print(f"Server at {args.url} is healthy")

    if args.discover:
        client.discover()
        return 0

    if args.skill:
        inp = json.loads(args.input)
        if args.poll:
            result = client.execute_and_poll(args.skill, inp)
        else:
            result = client.execute(args.skill, inp)
        print(json.dumps(result, indent=2, default=str))
        return 0

    print("Use --discover to list skills, or --skill <id> --input '{}' to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
