import re
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    cast,
    no_type_check,
)

import pydantic
from pydantic.fields import Field as PDField
from pydantic.fields import FieldInfo as PDFieldInfo
from pydantic.fields import Undefined
from pydantic.types import PyObject
from pydantic.typing import resolve_annotations

from odmantic.fields import ODMBaseField, ODMField, ODMFieldInfo
from odmantic.reference import ODMReference, ODMReferenceInfo

from .types import _SUBSTITUTION_TYPES, BSONSerializedField, _objectId

UNTOUCHED_TYPES = FunctionType, property, classmethod, staticmethod


def is_valid_odm_field(name: str) -> bool:
    return not name.startswith("__") and not name.endswith("__")


def to_snake_case(s: str) -> str:
    tmp = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", s)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", tmp).lower()


def find_duplicate_key(fields: Sequence[ODMField]) -> Optional[str]:
    seen: Set[str] = set()
    for f in fields:
        if f.key_name in seen:
            return f.key_name
        seen.add(f.key_name)
    return None


class ModelMetaclass(pydantic.main.ModelMetaclass):
    @no_type_check
    def __new__(cls, name, bases, namespace, **kwargs):  # noqa C901
        if (namespace.get("__module__"), namespace.get("__qualname__")) != (
            "odmantic.model",
            "Model",
        ):
            print(cls)
            print(name)
            print(bases)
            print(namespace)
            annotations = resolve_annotations(
                namespace.get("__annotations__", {}), namespace.get("__module__")
            )
            primary_field: Optional[str] = None
            odm_fields: Dict[str, ODMBaseField] = {}
            references: List[str] = []
            bson_serialized_fields: Set[str] = set()

            # TODO handle class vars
            # Substitute bson types
            for k, v in annotations.items():
                subst_type = _SUBSTITUTION_TYPES.get(v)
                if subst_type is not None:
                    print(f"Subst: {v} -> {subst_type}")
                    annotations[k] = subst_type
            namespace["__annotations__"] = annotations
            for (field_name, field_type) in annotations.items():
                if not is_valid_odm_field(field_name) or (
                    isinstance(field_type, UNTOUCHED_TYPES) and field_type != PyObject
                ):
                    continue
                if BSONSerializedField in getattr(field_type, "__bases__", ()):
                    bson_serialized_fields.add(field_name)
                value = namespace.get(field_name, Undefined)
                if isinstance(value, ODMFieldInfo):
                    if value.primary_field:
                        # TODO handle inheritance with primary keys
                        if primary_field is not None:
                            raise TypeError(
                                f"cannot define multiple primary keys on model {name}"
                            )
                        primary_field = field_name

                    key_name = (
                        value.key_name if value.key_name is not None else field_name
                    )
                    odm_fields[field_name] = ODMField(
                        primary_field=value.primary_field, key_name=key_name
                    )
                    namespace[field_name] = value.pydantic_field_info

                elif isinstance(value, ODMReferenceInfo):
                    if not issubclass(field_type, Model):
                        raise TypeError(
                            f"cannot define a reference {field_name} (in {name}) on "
                            "a model not created with odmantic.Model"
                        )
                    key_name = (
                        value.key_name if value.key_name is not None else field_name
                    )
                    odm_fields[field_name] = ODMReference(
                        model=field_type, key_name=key_name
                    )
                    references.append(field_name)
                elif value is Undefined:
                    odm_fields[field_name] = ODMField(
                        primary_field=False, key_name=field_name
                    )

                elif value is PDFieldInfo:
                    raise TypeError(
                        "please use odmantic.Field instead of pydantic.Field"
                    )

                else:
                    raise TypeError(f"Unhandled field definition {name}:{value}")

            for field_name, value in namespace.items():
                # TODO check referecnes defined without type
                # TODO find out what to do with those fields
                if (
                    field_name in annotations
                    or not is_valid_odm_field(field_name)
                    or isinstance(value, UNTOUCHED_TYPES)
                ):
                    continue
                odm_fields[field_name] = ODMField(
                    primary_field=False, key_name=field_name
                )
            if primary_field is None:
                primary_field = "id"
                odm_fields["id"] = ODMField(primary_field=True, key_name="_id")
                namespace["id"] = PDField(default_factory=_objectId)
                namespace["__annotations__"]["id"] = _objectId

            duplicate_key = find_duplicate_key(odm_fields.values())
            if duplicate_key is not None:
                raise TypeError(f"Duplicate key_name: {duplicate_key} in {name}")

            namespace["__odm_fields__"] = odm_fields
            namespace["__references__"] = tuple(references)
            namespace["__primary_key__"] = primary_field
            namespace["__bson_serialized_fields__"] = frozenset(bson_serialized_fields)
            if "__collection__" not in namespace:
                cls_name = name
                if cls_name.endswith("Model"):
                    # TODO document this
                    cls_name = cls_name[:-5]  # Strip Model in the class name
                namespace["__collection__"] = to_snake_case(cls_name)

        return super().__new__(cls, name, bases, namespace, **kwargs)


T = TypeVar("T", bound="Model")


class Model(pydantic.BaseModel, metaclass=ModelMetaclass):
    if TYPE_CHECKING:
        __collection__: ClassVar[str] = ""
        __primary_key__: ClassVar[str] = ""
        __odm_fields__: ClassVar[Dict[str, ODMBaseField]] = {}
        __bson_serialized_fields__: ClassVar[FrozenSet[str]] = frozenset()
        __references__: ClassVar[Tuple[str, ...]] = ()
        id: _objectId

    __slots__ = ()

    def __init_subclass__(cls):
        for name, field in cls.__odm_fields__.items():
            setattr(cls, name, field)

    @classmethod
    def parse_doc(cls: Type[T], raw_doc: Dict) -> T:
        doc: Dict[str, Any] = {}
        for field_name, field in cls.__odm_fields__.items():
            if isinstance(field, ODMReference):
                doc[field_name] = field.model.parse_doc(raw_doc[field.key_name])
            else:
                doc[field_name] = raw_doc[field.key_name]
        return cast(T, cls.parse_obj(doc))

    def doc(self) -> Dict[str, Any]:
        """
        Generate a document representation of the instance (as a dictionary)
        """
        raw_doc = self.dict()
        doc: Dict[str, Any] = {}
        for field_name, field in self.__odm_fields__.items():
            if isinstance(field, ODMReference):
                doc[field.key_name] = raw_doc[field_name]["id"]
            else:
                print(self.__bson_serialized_fields__)
                if field_name in self.__bson_serialized_fields__:
                    doc[field.key_name] = self.__fields__[field_name].type_.to_bson(
                        raw_doc[field_name]
                    )
                else:
                    doc[field.key_name] = raw_doc[field_name]
        return doc


class EmbeddedModel(pydantic.BaseModel):
    ...