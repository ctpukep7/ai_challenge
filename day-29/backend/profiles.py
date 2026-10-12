import copy

OPTION_KEYS = ("temperature", "num_predict", "num_ctx", "top_k", "top_p", "min_p", "repeat_penalty", "repeat_last_n", "seed", "stop", "presence_penalty")
SAMPLING_DEFAULTS = {"top_k": 20, "top_p": .8, "min_p": 0.0, "repeat_penalty": 1.0, "repeat_last_n": 64, "seed": -1, "stop": []}
BASELINE = {"temperature": .7, "num_predict": 1024, "num_ctx": 4096, **copy.deepcopy(SAMPLING_DEFAULTS)}
OPTIMIZED = {"temperature": 0, "num_predict": 1024, "num_ctx": 4096, "top_k": 20,
             "top_p": .8, "min_p": 0, "repeat_penalty": 1, "repeat_last_n": 64,
             "seed": -1, "stop": [], "presence_penalty": 0}
OPTIMIZED_SYSTEM = ''

LIMITS = {
    "temperature": {"min": 0, "max": 2}, "num_predict": [256, 512, 768, 1024, 1536, 2048],
    "num_ctx": [3072, 4096, 6144, 8192], "top_k": {"min": 0, "max": 100},
    "top_p": {"min": 0, "max": 1}, "min_p": {"min": 0, "max": 1},
    "repeat_penalty": {"min": .8, "max": 1.5}, "repeat_last_n": {"min": 0, "max": 2048},
    "seed": {"min": -1, "max": 2147483647}, "presence_penalty": {"min": 0, "max": 2},
    "stop": {"max_items": 4, "max_chars": 80, "max_bytes": 256},
    "system": {"max_chars": 3000, "max_bytes": 8000},
    "format": {"max_chars": 5000, "max_bytes": 12000, "max_depth": 8, "max_nodes": 256},
}


def effective_parameters(parameters):
    """Default the new native option without altering frozen legacy definitions."""
    return {"presence_penalty": 0, "format": None, **copy.deepcopy(parameters)}


def profile(identifier, settings=None):
    if identifier == "baseline":
        return {"id": identifier, "label": "Исходная модель", "system": "",
                "parameters": {**effective_parameters(BASELINE), "format": None, "keep_alive": "1m"}}
    if identifier != "optimized":
        raise ValueError("Unknown profile")
    settings = settings or {}
    parameters = effective_parameters(OPTIMIZED)
    parameters.update(copy.deepcopy({key: value for key, value in settings.items() if key in OPTION_KEYS}))
    parameters["format"] = copy.deepcopy(settings.get("format"))
    return {"id": identifier, "label": "Настроенная модель", "system": settings.get("system", OPTIMIZED_SYSTEM),
            "parameters": {**parameters, "keep_alive": "1m"}}
