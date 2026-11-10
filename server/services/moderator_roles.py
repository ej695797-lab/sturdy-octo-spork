"""Client-visible moderator roles are limited to the configured owner account."""

OWNER_MODERATOR_ID = "o_25596005490046939"
MODERATOR_ROLE_KEYS = {"moderator", "moderator_junior"}


def client_role_keys(account_id: str, role_keys) -> list:
    roles = list(role_keys or [])
    if account_id == OWNER_MODERATOR_ID:
        if "moderator" not in roles:
            roles.append("moderator")
        return roles
    return [role for role in roles if role not in MODERATOR_ROLE_KEYS]
