"""Permissions au moment du retrieval : par rôle et/ou domaine/version.

Le rôle de l'appelant est mappé (via ROLE_POLICY) vers un ensemble de domaines
et/ou versions autorisés. Ce filtre est combiné (intersection) avec un éventuel
filtre explicite passé dans la requête, puis appliqué DANS le SQL de retrieval —
de sorte qu'un chunk non autorisé n'est jamais ni récupéré ni cité.
"""
from __future__ import annotations

from .config import settings


def _intersect(a: list[str] | None, b: list[str] | None) -> list[str] | None:
    """None = pas de restriction. Sinon intersection des deux listes."""
    if a is None:
        return b
    if b is None:
        return a
    return [x for x in a if x in b]


def resolve(role: str | None,
            domains: list[str] | None,
            versions: list[str] | None) -> tuple[list[str] | None, list[str] | None, dict]:
    """Renvoie (domaines_autorisés, versions_autorisées, info).

    - liste  -> restreindre à ces valeurs
    - None   -> aucune restriction sur cette dimension
    - []     -> rien d'autorisé (deny) -> retrieval vide
    """
    policy = settings.role_policy_parsed
    role_domains = role_versions = None
    denied = False

    if role and role in policy:
        role_domains = policy[role].get("domains")
        role_versions = policy[role].get("versions")
    elif role and role not in policy and settings.permissions_default == "deny":
        denied = True  # rôle inconnu + politique deny -> rien

    if denied:
        return [], [], {"role": role, "decision": "deny (rôle hors policy)"}

    eff_domains = _intersect(role_domains, domains)
    eff_versions = _intersect(role_versions, versions)
    info = {
        "role": role,
        "domaines_autorisés": eff_domains if eff_domains is not None else "tous",
        "versions_autorisées": eff_versions if eff_versions is not None else "toutes",
    }
    return eff_domains, eff_versions, info
