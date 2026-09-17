"""Source descriptors (``config/sources/*.yaml``) as a package resource.

``collectors.config.default_config_dir()`` resolves this package via
``importlib.resources.files("config.sources")``. The YAML files here are the
single authoritative copy; they are declared as package data in
pyproject.toml so they travel inside the wheel.
"""
