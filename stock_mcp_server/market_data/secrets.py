"""비밀값 운반체 (1.0 설계 11.2절).

일반 dict 대신 출력이 차단된 SecretPayload 로 자격 증명을 옮긴다.
- credential schema 에 없는 필드는 거부한다.
- repr/str/직렬화 어디에도 원문·일부·길이·hash 를 드러내지 않는다.
- 값 접근은 명시적 get() 하나뿐이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from stock_mcp_server.market_data.provider_registry import CredentialField


class SecretValidationError(Exception):
    """비밀 없는 검증 오류. 메시지에 값을 넣지 않는다."""


class SecretPayload:
    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]):
        # 직접 생성보다 from_schema 를 쓴다. 여기서는 복사만 한다.
        object.__setattr__(self, "_values", dict(values))

    @classmethod
    def from_schema(
        cls,
        schema: tuple["CredentialField", ...],
        values: Mapping[str, object],
    ) -> "SecretPayload":
        expected = [field.name for field in schema]
        provided = list(values.keys())

        unknown = sorted(set(provided) - set(expected))
        if unknown:
            raise SecretValidationError(
                f"schema에 없는 필드: {', '.join(unknown)}")
        missing = [name for name in expected if name not in values]
        if missing:
            raise SecretValidationError(
                f"필수 필드 누락: {', '.join(missing)}")

        cleaned: dict[str, str] = {}
        for field in schema:
            raw = values[field.name]
            if not isinstance(raw, str) or not raw.strip():
                raise SecretValidationError(
                    f"{field.name} 값이 비었거나 문자열이 아닙니다")
            if len(raw) > field.max_length:
                raise SecretValidationError(
                    f"{field.name} 값이 허용 길이를 초과했습니다")
            cleaned[field.name] = raw
        return cls(cleaned)

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError:
            raise SecretValidationError(
                f"payload에 없는 필드: {name}") from None

    def field_names(self) -> tuple[str, ...]:
        return tuple(self._values.keys())

    def __repr__(self) -> str:
        return "SecretPayload(***)"

    __str__ = __repr__

    def __setattr__(self, name, value):
        raise AttributeError("SecretPayload는 불변입니다")
