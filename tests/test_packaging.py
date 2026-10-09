from importlib.resources import files


def test_index_template_is_packaged():
    """The Jinja template must be installed as package data.

    The container only has the wheel - if `package-data` is missing from
    pyproject.toml, `GET /` fails with TemplateNotFound.
    """
    template = files("ha_satimage").joinpath("templates/index.html")
    assert template.is_file()
