"""PDF reflow: convert a multi-column desktop PDF into a single-column mobile PDF."""


def reflow_pdf(*args, **kwargs):
    from .reflow import reflow_pdf as _impl
    return _impl(*args, **kwargs)


def ReflowConfig(*args, **kwargs):
    from .reflow import ReflowConfig as _impl
    return _impl(*args, **kwargs)
