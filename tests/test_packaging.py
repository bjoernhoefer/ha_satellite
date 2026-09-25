from importlib.resources import files


def test_index_template_is_packaged():
    """Das Jinja-Template muss als Package-Data mitinstalliert werden.

    Im Container liegt nur das Wheel vor - fehlt `package-data` in der
    pyproject.toml, laeuft `GET /` in einen TemplateNotFound-Fehler.
    """
    template = files("ha_satellite").joinpath("templates/index.html")
    assert template.is_file()
