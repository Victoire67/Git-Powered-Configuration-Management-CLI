import gitconfig_cli

# imports the package inside the tests


def test_package_has_version() -> None:
    assert (
        gitconfig_cli.__version__ == "0.1.0"
    )  # Checks if the version inside the __init__ file is the correct one
