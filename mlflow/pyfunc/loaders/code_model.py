from typing import Any, Dict, Optional

from mlflow.pyfunc.loaders.chat_model import _ChatModelPyfuncWrapper
from mlflow.pyfunc.loaders.flexible_model import _OompaLoopaModelPyfuncWrapper
from mlflow.pyfunc.model import (
    _PythonModelPyfuncWrapper,
    ChatModel,
    _load_context_model_and_signature,
)


def _load_pyfunc(local_path: str, model_config: Optional[Dict[str, Any]] = None):
    context, model, signature = _load_context_model_and_signature(local_path, model_config)
    if isinstance(model, ChatModel):
        return _ChatModelPyfuncWrapper(model, context, signature)
    elif isinstance(model, OompaLoopaModel):
        return _OompaLoopaModelPyfuncWrapper(model, context, signature)
    else:
        return _PythonModelPyfuncWrapper(model, context, signature)
