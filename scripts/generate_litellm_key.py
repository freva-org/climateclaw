"""Generate a LiteLLM key for a user, allowing them to make requests to the vscode access group.
See the litellm_config.yaml file for the list of models that are available to the vscode access group.
The generated key has a 30-day lifetime and no budget limits.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# Defaults to 30 days, but can be overridden by the user in the request payload.
# (Also supports h for hours, check the LiteLLM documentation for more details.)
DEFAULT_KEY_DURATION = "30d"


def build_request_payload(
    models: list[str], user_id: str | None, duration: str
) -> dict[str, str | list[str]]:
    """Create the LiteLLM /key/generate payload."""

    payload: dict[str, str | list[str]] = {
        "duration": duration,
        "models": models,
    }

    if user_id:
        payload["user_id"] = user_id

    return payload


def main():
    parser = argparse.ArgumentParser(
        description="Generate a LiteLLM key restricted to the vscode access group."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,  # dotenv will search for it automatically if not provided
        help="Path to the .env file containing the LiteLLM master key.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://127.0.0.1:4000",  # It's by default supposed to be run from within the container with the LiteLLM proxy
        help="LiteLLM proxy base URL.",
    )
    parser.add_argument(
        "--user-id", type=str, default=None, help="Optional LiteLLM user_id."
    )
    parser.add_argument(
        "--duration",
        type=str,
        default=DEFAULT_KEY_DURATION,
        help="Duration of the generated key (e.g., '30d' for 30 days).",
    )
    parser.add_argument(
        "--models",
        type=str,  # This is correct for some reason.
        nargs="+",  # Expects one or more model names as arguments
        default=["vscode"],
        help="List of models to include in the generated key, space separated. Default is 'vscode'.",
    )
    args = parser.parse_args()

    if not args.env_file.exists():
        raise FileNotFoundError(f".env file not found: {args.env_file}")

    load_dotenv(args.env_file)

    master_key: str | None = os.getenv("FREVAGPT_LITE_LLM_MASTER_KEY") or os.getenv(
        "LITELLM_MASTER_KEY"
    )
    if not master_key:
        raise RuntimeError(
            "Missing LiteLLM master key. Set FREVAGPT_LITE_LLM_MASTER_KEY or LITELLM_MASTER_KEY in the .env file."
        )

    payload: dict[str, str | list[str]] = build_request_payload(
        models=args.models, user_id=args.user_id, duration=args.duration
    )

    headers: dict[str, str] = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    base_endpoint: str = args.base_url.rstrip("/")  # remove trailing slash if present
    endpoint: str = f"{base_endpoint}/key/generate"

    with httpx.Client(timeout=30.0) as client:
        response = client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()  # Raise an exception for HTTP errors (4xx and 5xx responses)
        result = response.json()

    print(json.dumps(result, indent=2, sort_keys=True))
    return


if __name__ == "__main__":
    main()
