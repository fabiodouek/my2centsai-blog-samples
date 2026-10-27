"""A data-science template: the base image plus pandas, numpy and matplotlib preinstalled."""

from e2b import Template

template = (
    Template()
    .from_base_image()
    .pip_install(["pandas", "numpy", "matplotlib"])
    .set_envs({"MPLBACKEND": "Agg"})
)
