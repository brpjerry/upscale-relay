"""Video options shared by clients and the server; no media dependencies."""

DEFAULT_RESIZE_ALGORITHM = "lanczos"
RESIZE_ALGORITHMS = (
    "fast-bilinear",
    "bilinear",
    "bicubic",
    "area",
    "bicublin",
    "gaussian",
    "sinc",
    "lanczos",
    "spline",
)
