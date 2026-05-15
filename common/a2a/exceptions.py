"""Base exceptions for the A2A client."""


class A2AError(Exception):
    """Base exception for all A2A client errors."""

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details = details or {}


class A2AConnectionError(A2AError):
    """Failed to connect to the A2A server (network, DNS, timeout)."""


class A2AAuthenticationError(A2AError):
    """Authentication failed — token invalid, expired, or missing for protected skill."""


class A2ASkillNotFoundError(A2AError):
    """The requested skill_id does not exist on the server."""


class A2ASkillExecutionError(A2AError):
    """Skill executed but returned success=False."""

    def __init__(self, message: str, response: dict = None):
        super().__init__(message)
        self.response = response or {}


class A2AWorkflowTimeoutError(A2AError):
    """Workflow polling exceeded the timeout without reaching a terminal state."""

    def __init__(self, message: str, last_status: dict = None):
        super().__init__(message)
        self.last_status = last_status


class A2AWorkflowNotFoundError(A2AError):
    """The requested workflow_id was not found on the server."""

    def __init__(self, message: str, workflow_id: str = None):
        super().__init__(message)
        self.workflow_id = workflow_id


class A2AValidationError(A2AError):
    """Server rejected the request due to schema validation (400)."""
