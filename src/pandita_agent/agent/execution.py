"""Operator-owned vLLM request profiles, independent of serving infrastructure."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from pandita_agent.contracts import Contract, Identifier


class Sampling(Contract):
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    top_k: int | None = Field(default=None, ge=-1)
    min_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    repetition_penalty: float | None = Field(default=None, gt=0, le=2, allow_inf_nan=False)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2, allow_inf_nan=False)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2, allow_inf_nan=False)
    seed: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    stop: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] | None = Field(
        default=None, max_length=4
    )


class ExecutionProfile(Contract):
    id: Identifier
    backend: Literal["vllm"] = "vllm"
    family: Literal["gemma4", "qwen3_8"]
    sampling: Sampling = Field(default_factory=Sampling)
    enable_thinking: bool = True
    reasoning_effort: Literal["low", "medium", "xhigh"] | None = None

    @model_validator(mode="after")
    def supported_controls(self):
        if self.reasoning_effort is not None and (
            self.family != "qwen3_8" or self.enable_thinking is not True
        ):
            raise ValueError("Reasoning effort requires explicitly enabled Qwen3.8 thinking.")
        return self

    def request_options(self):
        # Raw HTTP extensions are top-level JSON, not an OpenAI SDK extra_body envelope.
        options = self.sampling.model_dump(mode="json", exclude_none=True)
        # Gemma history is filtered by the harness before the template sees it.
        # Preserve the surviving tool-call thoughts, including those in earlier turns.
        options["chat_template_kwargs"] = {
            "enable_thinking": self.enable_thinking,
            "preserve_thinking": True,
        }
        if self.reasoning_effort is not None:
            options["reasoning_effort"] = self.reasoning_effort
        return options
