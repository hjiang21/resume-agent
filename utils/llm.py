"""One synchronous OpenAI client for structured outputs and plain text."""

import os
from typing import TypeVar

from openai import APIConnectionError, APIError, APIStatusError, OpenAI
from pydantic import BaseModel, ValidationError

Output = TypeVar("Output", bound=BaseModel)


class ModelError(RuntimeError):
    """Readable model failure without dumping candidate data or API secrets."""


class LLM:
    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "").strip()
        if not api_key:
            raise ModelError("Set OPENAI_API_KEY in .env or your environment.")
        if not self.model:
            raise ModelError("Set OPENAI_MODEL to a model supporting structured outputs.")
        self.client = OpenAI(api_key=api_key, timeout=120.0, max_retries=2)

    def structured(self, instructions: str, content: str,
                   schema: type[Output]) -> Output:
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[{"role": "system", "content": instructions},
                       {"role": "user", "content": content}],
                text_format=schema,
            )
            if response.status != "completed" or response.output_parsed is None:
                raise ModelError("Model returned no completed structured result (refusal or incomplete response).")
            return schema.model_validate(response.output_parsed.model_dump())
        except APIConnectionError:
            raise ModelError("Could not connect to OpenAI (connection failure or timeout).") from None
        except APIStatusError as exc:
            raise ModelError(f"OpenAI API failed (HTTP {exc.status_code}). Check credentials, model access, and quota.") from None
        except APIError:
            raise ModelError("OpenAI API request failed. Retry or check your configuration.") from None
        except ValidationError:
            raise ModelError(f"Model output failed structured validation for {schema.__name__}.") from None

    def text(self, instructions: str, content: str) -> str:
        try:
            response = self.client.responses.create(
                model=self.model,
                input=[{"role": "system", "content": instructions},
                       {"role": "user", "content": content}],
            )
            if response.status != "completed":
                raise ModelError("Model returned an incomplete or failed text response.")
            if any(part.type == "refusal" for item in response.output
                   if item.type == "message" for part in item.content):
                raise ModelError("Model refused to generate the requested text.")
            result = response.output_text
            if not isinstance(result, str) or not result.strip():
                raise ModelError("Model returned blank text; existing output was not replaced.")
            return result
        except APIConnectionError:
            raise ModelError("Could not connect to OpenAI (connection failure or timeout).") from None
        except APIStatusError as exc:
            raise ModelError(f"OpenAI API failed (HTTP {exc.status_code}). Check credentials, model access, and quota.") from None
        except APIError:
            raise ModelError("OpenAI API request failed. Retry or check your configuration.") from None

    def close(self) -> None:
        self.client.close()
