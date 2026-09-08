from typing import Any

from pydantic import BaseModel, Field


class CreatedFile(BaseModel):
    path: str
    mime_type: str
    preview_url: str | None = None
    url_sent_to_model: bool = False


class CodeInterpreterResult(BaseModel):
    stdout: str = ""
    stderr: str = ""
    result_repr: str = ""
    display_data: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_files: list[CreatedFile] = Field(default_factory=list)

    @property
    def output_text(self) -> str:
        parts = []

        if self.stdout:
            parts.append(self.stdout)

        if self.result_repr:
            parts.append(self.result_repr)

        return "\n".join(parts)

    @property
    def error_text(self) -> str:
        parts = []

        if self.stderr:
            parts.append(self.stderr)

        if self.error:
            parts.append(self.error)

        return "\n".join(parts)

    @property
    def is_error(self) -> bool:
        return bool(self.stderr or self.error)


class GenericToolResult(BaseModel):
    result: str = ""
    error: str = ""
