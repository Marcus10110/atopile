# pylint: disable=logging-fstring-interpolation

"""
This CLI command provides the `ato install` command to:
- install dependencies
- download JLCPCB footprints
"""

import logging
import subprocess
from itertools import chain
from pathlib import Path
from typing import Optional

import click
import yaml
from git import InvalidGitRepositoryError, NoSuchPathError, Repo

from atopile import config, errors, version

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


@click.command("install")
@click.argument("to_install", required=False)
@click.option("--jlcpcb", is_flag=True, help="JLCPCB component ID")
@click.option("--upgrade", is_flag=True, help="Upgrade dependencies")
def install(to_install: str, jlcpcb: bool, upgrade: bool):
    """
    Install a dependency of for the project.
    """
    repo = Repo(".", search_parent_directories=True)
    top_level_path = Path(repo.working_tree_dir)

    if jlcpcb:
        # eg. "ato install --jlcpcb=C123"
        install_jlcpcb(to_install)
    elif to_install:
        # eg. "ato install some-atopile-module"
        installed_semver = install_dependency(
            to_install,
            top_level_path,
            upgrade
        )
        module_name, module_spec = split_module_spec(to_install)
        if module_spec is None and installed_semver:
            # If the user didn't specify a version, we'll
            # use the one we just installed as a basis
            to_install = f"{module_name}^{installed_semver}"
        set_dependency_to_ato_yaml(top_level_path, to_install)

    else:
        # eg. "ato install"
        install_dependencies_from_yaml(top_level_path, upgrade)

    log.info("[green]Done![/] :call_me_hand:", extra={"markup": True})


def split_module_spec(spec: str) -> tuple[str, Optional[str]]:
    """Splits a module spec string into the module name and the version spec."""
    for splitter in chain(version.OPERATORS, (" ", "@")):
        if splitter in spec:
            splitter_loc = spec.find(splitter)
            if splitter_loc < 0:
                continue

            module_name = spec[:splitter_loc].strip()
            version_spec = spec[splitter_loc:].strip()
            return module_name, version_spec

    return spec, None


def set_dependency_to_ato_yaml(top_level_path: Path, module_spec: str):
    """Add deps to the ato.yaml file"""
    # Get the existing config data
    ato_yaml_path = top_level_path / "ato.yaml"
    if not ato_yaml_path.exists():
        raise errors.AtoError(f"ato.yaml not found in {top_level_path}")

    with ato_yaml_path.open("r") as file:
        data = yaml.safe_load(file) or {}

    # Add module to dependencies, avoiding duplicates
    dependencies: list[str] = data.setdefault("dependencies", [])
    dependencies_by_name: dict[str, str] = {
        split_module_spec(x)[0]: x for x in dependencies
    }

    module_to_install, _ = split_module_spec(module_spec)
    if module_to_install in dependencies_by_name:
        existing_spec = dependencies_by_name[module_to_install]
        if existing_spec != module_spec:
            dependencies.remove(existing_spec)

    if module_spec not in dependencies:
        dependencies.append(module_spec)

        with ato_yaml_path.open("w") as file:
            yaml.safe_dump(data, file, default_flow_style=False)


def install_dependency(
    module: str, top_level_path: Path, upgrade: bool = False
) -> Optional[version.Version]:
    """
    Install a dependency of the name "module_name"
    into the project to "top_level_path"
    """
    # Figure out what we're trying to install here
    module_name, module_spec = split_module_spec(module)
    if not module_spec:
        module_spec = "*"

    # Ensure the modules path exists
    modules_path = config.get_project_config_from_path(
        top_level_path
    ).paths.abs_module_path
    modules_path.mkdir(parents=True, exist_ok=True)

    try:
        # This will raise an exception if the directory does not exist
        repo = Repo(modules_path / module_name)
    except (InvalidGitRepositoryError, NoSuchPathError):
        # Directory does not contain a valid repo, clone into it
        log.info(f"Installing dependency {module_name}")
        clone_url = f"https://gitlab.atopile.io/packages/{module_name}"
        repo = Repo.clone_from(clone_url, modules_path / module_name)
    else:
        # In this case the directory exists and contains a valid repo
        if upgrade:
            log.info(f"Fetching latest changes for {module_name}")
            repo.remotes.origin.fetch()
        else:
            log.info(
                f"{module_name} already exists. If you wish to upgrade, use --upgrade"
            )
            # here we're done because we don't want to play with peoples' deps under them
            return

    # Figure out what version of this thing we need
    semver_to_tag = {}
    installed_semver = None
    for tag in repo.tags:
        try:
            semver_to_tag[version.parse(tag.name)] = tag
        except errors.AtoError:
            log.debug(f"Tag {tag.name} is not a valid semver tag. Skipping.")

    if "@" in module_spec:
        # If there's an @ in the version, we're gonna check that thing out
        best_checkout = module_spec.strip().strip("@")
    elif semver_to_tag:
        # Otherwise we're gonna find the best tag meeting the semver spec
        valid_versions = [v for v in semver_to_tag if version.match(module_spec, v)]
        installed_semver = max(valid_versions)
        best_checkout = semver_to_tag[installed_semver]
    else:
        log.warning(
            "No semver tags found for this module. Using latest default branch :hot_pepper:.",
            extra={"markup": True},
        )

    # If the repo is dirty, throw an error
    if repo.is_dirty():
        raise errors.AtoError(
            f"Module {module_name} has uncommitted changes. Aborting."
        )

    # Checkout the best thing we've found
    commit_before_checkout = repo.head.commit
    repo.git.checkout(best_checkout)
    if repo.head.commit == commit_before_checkout:
        log.info(
            f"Already on the best option ([cyan bold]{best_checkout}[/]) for {module_name}",
            extra={"markup": True},
        )
    else:
        log.info(
            f"Using :sparkles: [cyan bold]{best_checkout}[/] :sparkles: of {module_name}",
            extra={"markup": True},
        )

    if installed_semver:
        return best_checkout


def install_dependencies_from_yaml(top_level_path: Path, upgrade: bool = False):
    """Install all dependencies from the ato.yaml file"""
    cfg = config.get_project_config_from_path(top_level_path)
    for module_name in cfg.dependencies:
        install_dependency(module_name, top_level_path, upgrade)


def install_jlcpcb(component_id: str):
    """Install a component from JLCPCB"""
    component_id = component_id.upper()
    if not component_id.startswith("C") or not component_id[1:].isdigit():
        raise errors.AtoError(f"Component id {component_id} is invalid. Aborting.")

    # Get the top level of the git module the user is currently within
    repo = Repo(".", search_parent_directories=True)
    top_level_path = Path(repo.working_tree_dir)

    # Get the remote URL
    remote_url = repo.remote().url

    # Set the footprints_dir based on the remote URL
    if remote_url == "git@gitlab.atopile.io:atopile/modules.git":
        footprints_dir = top_level_path / "footprints"
    else:
        footprints_dir = top_level_path / "elec/footprints/footprints"

    log.info(f"Footprints directory: {footprints_dir}")

    command = [
        "easyeda2kicad",
        "--full",
        f"--lcsc_id={component_id}",
        f"--output={footprints_dir}",
        "--overwrite",
        "--ato",
        f"--ato_file_path={top_level_path / 'elec/src'}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    # The stdout and stderr are captured due to 'capture_output=True'
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)

    # Check the return code to see if the command was successful
    if result.returncode == 0:
        print("Command executed successfully")
    else:
        raise errors.AtoError("Couldn't install component")