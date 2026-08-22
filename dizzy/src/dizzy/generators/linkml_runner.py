"""LinkML runner — shells out to gen-pydantic / gen-sqla / gen-rust / gen-typescript /
gen-json-schema."""

import subprocess
from pathlib import Path

from dizzy.logger import logger


def run_linkml_pydantic(def_file: Path, output_file: Path) -> None:
    """Run gen-pydantic on def_file and write the result to output_file."""
    result = subprocess.run(
        ["gen-pydantic", str(def_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    logger.debug(
        "gen-pydantic completed",
        extra={"command": "gen-pydantic", "input": str(def_file), "output": str(output_file)},
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(result.stdout)


def run_linkml_sqla(def_file: Path, output_file: Path) -> None:
    """Run gen-sqla on def_file and write the result to output_file."""
    result = subprocess.run(
        ["gen-sqla", str(def_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    logger.debug(
        "gen-sqla completed",
        extra={"command": "gen-sqla", "input": str(def_file), "output": str(output_file)},
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(result.stdout)


def run_linkml_json_schema(def_file: Path, output_file: Path) -> None:
    """Run gen-json-schema on def_file and write the result to output_file.

    Every class in the schema lands under ``$defs``; validate a single payload with
    ``{"$ref": "#/$defs/<ClassName>"}`` against the emitted document.
    """
    result = subprocess.run(
        ["gen-json-schema", str(def_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    logger.debug(
        "gen-json-schema completed",
        extra={"command": "gen-json-schema", "input": str(def_file), "output": str(output_file)},
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(result.stdout)


def run_linkml_rust(def_file: Path, output_file: Path) -> None:
    """Run gen-rust (file mode, serde) on def_file and write the result to output_file."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["gen-rust", "-m", "file", "-s", "--force", "-o", str(output_file), str(def_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    logger.debug(
        "gen-rust completed",
        extra={"command": "gen-rust", "input": str(def_file), "output": str(output_file)},
    )


def run_linkml_typescript(def_file: Path, output_file: Path) -> None:
    """Run gen-typescript on def_file and write the result to output_file."""
    result = subprocess.run(
        ["gen-typescript", str(def_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    logger.debug(
        "gen-typescript completed",
        extra={"command": "gen-typescript", "input": str(def_file), "output": str(output_file)},
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(result.stdout)
