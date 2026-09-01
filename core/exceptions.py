class UkoreHubError(Exception):
    pass


class ValidationError(UkoreHubError):
    pass


class NotFoundError(UkoreHubError):
    pass


class GitHubAuthError(UkoreHubError):
    pass
