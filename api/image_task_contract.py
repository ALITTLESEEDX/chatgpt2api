from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ImageTaskStatus = Literal[
    "queued",
    "running",
    "success",
    "partial_success",
    "failed",
    "text_review",
]
ImageTaskMode = Literal["generate", "edit"]


class ImageTaskAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = ""
    path: str = ""
    b64_json: str = ""
    revised_prompt: str = ""
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class ImageTaskActions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_poll: bool


class ImageTaskPartialFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: int | None = None
    code: str = ""
    message: str = ""
    status_code: int | None = None


class ImageTaskRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: ImageTaskStatus
    terminal: bool
    mode: ImageTaskMode
    model: str
    size: str
    quality: str
    stage_code: str
    stage_label: str
    created_at: str
    updated_at: str
    requested_count: int = Field(ge=1, le=4)
    succeeded_count: int = Field(ge=0, le=4)
    failed_count: int = Field(ge=0, le=4)
    pending_count: int = Field(ge=0, le=4)
    duration_ms: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    error_code: str
    public_error: str
    partial_failures: list[ImageTaskPartialFailure] = Field(default_factory=list)
    results: list[ImageTaskAsset] = Field(default_factory=list)
    actions: ImageTaskActions


class ImageTaskPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ImageTaskRow]
    missing_ids: list[str] = Field(default_factory=list)
    # 不带 ids 的列表查询会返回分页信息；按 id 精确查询（轮询）不带这些字段。
    total: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    page_size: int | None = Field(default=None, ge=1)
    page_count: int | None = Field(default=None, ge=1)
