import os
from typing import Any

import streamlit as st


SECRET_SECTIONS = ("general", "secrets", "connections")


def _get_secrets_dict() -> dict[str, Any]:
    try:
        return st.secrets.to_dict()
    except Exception:
        return {}


def get_config_value(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value.strip()

    secrets = _get_secrets_dict()
    candidate_names = (name, name.lower())

    for candidate in candidate_names:
        value = secrets.get(candidate)
        if value:
            return str(value).strip()

    for section in SECRET_SECTIONS:
        section_values = secrets.get(section)
        if not isinstance(section_values, dict):
            continue
        for candidate in candidate_names:
            value = section_values.get(candidate)
            if value:
                return str(value).strip()

    return default


def missing_config_message(name: str) -> str:
    secrets = _get_secrets_dict()
    secret_keys = ", ".join(sorted(secrets.keys())) or "aucune"
    env_status = "présente" if os.getenv(name) else "absente"

    return (
        f"{name} est manquant.\n\n"
        "Vérifie ton fichier `.env` en local ou les Secrets Streamlit Cloud.\n\n"
        f"Diagnostic sans valeurs sensibles : variable d'environnement {env_status}; "
        f"clés Streamlit visibles : {secret_keys}."
    )
