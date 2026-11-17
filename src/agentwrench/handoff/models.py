"""Versioned, strict manifest models. Task text is always an unverified claim."""

from __future__ import annotations

import base64
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .io import HandoffError, canonical, command_argv, digest, relative_path

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Oid = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Text = Annotated[str, Field(min_length=1, max_length=16000)]
Arg = Annotated[str, Field(max_length=16000)]
PathText = Annotated[str, Field(min_length=1, max_length=4096)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Task(StrictModel):
    goal: Text
    claimed_done: list[Text] = Field(default_factory=list, max_length=1000)
    unresolved: list[Text] = Field(default_factory=list, max_length=1000)
    artifacts: list[PathText] = Field(default_factory=list, max_length=1000)
    verification_commands: list[list[Arg]] = Field(default_factory=list, max_length=100)

    @field_validator("goal")
    @classmethod
    def meaningful_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task goal cannot be blank")
        return value

    @field_validator("artifacts")
    @classmethod
    def paths(cls, values: list[str]) -> list[str]:
        for value in values:
            relative_path(value)
        if len(values) != len(set(values)):
            raise ValueError("duplicate artifact path")
        return values

    @field_validator("verification_commands")
    @classmethod
    def commands(cls, values: list[list[str]]) -> list[list[str]]:
        for argv in values:
            command_argv(argv)
        return values


class Blob(StrictModel):
    encoding: Literal["base64"] = "base64"
    sha256: Hash
    size: int = Field(ge=0, le=8 * 1024 * 1024)
    data: str = Field(max_length=12 * 1024 * 1024)

    @classmethod
    def from_bytes(cls, data: bytes) -> Blob:
        return cls(sha256=digest(data), size=len(data), data=base64.b64encode(data).decode("ascii"))

    def bytes(self) -> bytes:
        try:
            data = base64.b64decode(self.data, validate=True)
        except ValueError as exc:
            raise HandoffError("invalid base64 evidence") from exc
        if len(data) != self.size or digest(data) != self.sha256:
            raise HandoffError("embedded evidence hash or length mismatch")
        return data


class FileState(StrictModel):
    path: PathText
    kind: Literal["file", "symlink", "missing"]
    sha256: Hash | None
    executable: bool = False

    @field_validator("path")
    @classmethod
    def path_safe(cls, value: str) -> str:
        return relative_path(value)

    @model_validator(mode="after")
    def kind_hash(self) -> FileState:
        if (self.kind == "missing") != (self.sha256 is None):
            raise ValueError("only missing files have a null hash")
        if self.kind != "file" and self.executable:
            raise ValueError("executable applies only to regular files")
        return self


class IndexEntry(StrictModel):
    path: PathText
    mode: Literal["100644", "100755", "120000"]
    oid: Oid

    @field_validator("path")
    @classmethod
    def path_safe(cls, value: str) -> str:
        return relative_path(value)


class Snapshot(StrictModel):
    # Populated only by a live local snapshot, never loaded from manifest JSON.
    _observed_stats: dict[str, tuple[tuple[int, ...] | None, int]] = PrivateAttr(default_factory=dict)
    scope: Literal["git-tracked-and-nonignored-v1"] = "git-tracked-and-nonignored-v1"
    head: Oid
    branch_hint: str | None
    files: list[FileState] = Field(max_length=10000)
    index: list[IndexEntry] = Field(max_length=10000)
    staged_diff: Blob
    unstaged_diff: Blob
    state_sha256: Hash

    def state_data(self) -> dict:
        value = self.model_dump(exclude={"state_sha256", "branch_hint"})
        # Patch bytes remain in the bundle but only their digest enters the state ID.
        for field in ("staged_diff", "unstaged_diff"):
            value[field].pop("data")
        return value

    def check(self) -> None:
        self.staged_diff.bytes()
        self.unstaged_diff.bytes()
        for items in (self.files, self.index):
            paths = [item.path for item in items]
            if paths != sorted(set(paths)):
                raise HandoffError("snapshot paths must be unique and sorted")
        if digest(canonical(self.state_data())) != self.state_sha256:
            raise HandoffError("source-state digest mismatch")


class LocalEvidence(StrictModel):
    source_hint: str = Field(max_length=4096)
    format: Literal["in-toto"] = "in-toto"
    content: Blob


class ChangeRange(StrictModel):
    base: Oid
    committed_diff: Blob


class VerificationCommand(StrictModel):
    purpose: Text
    argv: list[Arg]

    @field_validator("argv")
    @classmethod
    def valid_command(cls, value: list[str]) -> list[str]:
        return command_argv(value)


class Manifest(StrictModel):
    schema_version: Literal["1"] = "1"
    created_at: str
    task_claims: Task
    snapshot: Snapshot
    change_range: ChangeRange | None = None
    supplied_diff: Blob | None = None
    artifacts: list[FileState] = Field(default_factory=list, max_length=10000)
    evidence: list[LocalEvidence] = Field(default_factory=list, max_length=100)
    verification: list[VerificationCommand] = Field(min_length=1, max_length=101)
    limitations: list[str]
    integrity_sha256: Hash

    def seal(self) -> None:
        self.integrity_sha256 = digest(canonical(self.model_dump(exclude={"integrity_sha256"})))

    def check(self) -> None:
        actual = digest(canonical(self.model_dump(exclude={"integrity_sha256"})))
        if actual != self.integrity_sha256:
            raise HandoffError("handoff integrity mismatch")
        self.snapshot.check()
        if self.change_range:
            self.change_range.committed_diff.bytes()
        if self.supplied_diff:
            self.supplied_diff.bytes()
        for evidence in self.evidence:
            evidence.content.bytes()
        paths = [item.path for item in self.artifacts]
        if paths != sorted(set(paths)):
            raise HandoffError("manifest artifact paths must be unique and sorted")
