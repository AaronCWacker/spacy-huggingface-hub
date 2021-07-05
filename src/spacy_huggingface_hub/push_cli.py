import os
import typer
import zipfile
import shutil
import json
import yaml
from huggingface_hub import Repository, HfApi, HfFolder
from typing import Optional
from pathlib import Path
from spacy.cli._util import parse_config_overrides, Arg, Opt, app

import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

SPACY_HF_HUB_HELP = """CLI for uploading pipelines to the 
Hugging Face Hub (hf.co).
"""

NAME = "spacy"
HELP = """spaCy Command-line Interface
DOCS: https://spacy.io/api/cli
"""

# Create our subcommand, and install it within spaCy's CLI
hf_hub_cli = typer.Typer(
    name="huggingface_hub", help=SPACY_HF_HUB_HELP, no_args_is_help=True
)
app.add_typer(hf_hub_cli)


@hf_hub_cli.command(
    "push", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def huggingface_hub_push_cli(
    # fmt: off
    whl_path: Path = typer.Argument(..., help="Path to whl file", exists=True),
    organization: Optional[str] = typer.Option(None, help="Name of organization to which the pipeline should be uploaded."),
    # fmt: on
):
    """
    Push a spaCy pipeline to the Hugging Face Hub.
    """
    push(whl_path, organization)


token_classification_components = ["ner", "tagger", "morphologizer"]
text_classification_components = ["textcat", "textcat_multilabel"]


def _insert_value(metadata, name, value):
    if value is None or value == "":
        return metadata
    metadata[name] = value
    return metadata


def _insert_values_as_list(metadata, name, values):
    if values is None:
        return metadata
    if isinstance(values, str):
        values = [values]
    if len(values) == 0:
        return metadata
    metadata[name] = values
    return metadata


def _create_metric(name, t, value):
    return {"name": name, "type": t, "value": value}


def _create_p_r_f_list(precision, recall, f_score):
    precision = _create_metric("Precision", "precision", precision)
    recall = _create_metric("Recall", "recall", recall)
    f_score = _create_metric("F Score", "f_score", f_score)

    return [precision, recall, f_score]


def _create_model_index(repo_name, data):
    model_index = {"name": repo_name}

    results = []
    if "ents_p" in data:
        results.append(
            {
                "tasks": {
                    "name": "NER",
                    "type": "token-classification",
                    "metrics": _create_p_r_f_list(
                        data["ents_p"], data["ents_r"], data["ents_f"]
                    ),
                }
            }
        )
    if "tag_acc" in data:
        results.append(
            {
                "tasks": {
                    "name": "POS",
                    "type": "token-classification",
                    "metrics": [
                        _create_metric("Accuracy", "accuracy", data["tag_acc"])
                    ],
                }
            }
        )
    if "sents_p" in data:
        results.append(
            {
                "tasks": {
                    "name": "SENTER",
                    "type": "token-classification",
                    "metrics": _create_p_r_f_list(
                        data["sents_p"], data["sents_r"], data["sents_f"]
                    ),
                }
            }
        )
    if "dep_uas" in data:
        results.append(
            {
                "tasks": {
                    "name": "UNLABELED_DEPENDENCIES",
                    "type": "token-classification",
                    "metrics": [
                        _create_metric("Accuracy", "accuracy", data["dep_uas"])
                    ],
                }
            }
        )
    if "dep_las" in data:
        results.append(
            {
                "tasks": {
                    "name": "LABELED_DEPENDENCIES",
                    "type": "token-classification",
                    "metrics": [
                        _create_metric("Accuracy", "accuracy", data["dep_uas"])
                    ],
                }
            }
        )

    model_index["results"] = results
    return model_index


def _create_model_card(repo_name, repo_dir):
    with open(os.path.join(repo_dir, "meta.json")) as f:
        data = json.load(f)
        lang = data["lang"] if data["lang"] != "xx" else "multilingual"
        lic = data.get("license", "").replace(" ", "-")
        tags = ["spacy"]
        for component in data["components"]:
            if (
                component in token_classification_components
                and "token-classification" not in tags
            ):
                tags.append("token-classification")
            if (
                component in text_classification_components
                and "text-classification" not in tags
            ):
                tags.append("text-classification")

        metadata = _insert_values_as_list({}, "tags", tags)
        metadata = _insert_values_as_list(metadata, "language", lang)
        metadata = _insert_value(metadata, "license", lic)
        metadata["model-index"] = _create_model_index(repo_name, data["performance"])
        metadata = yaml.dump(metadata, sort_keys=False)
        metadata_section = f"---\n{metadata}---\n"

        # Read README generated by package
        readme_path = os.path.join(repo_dir, "README.md")
        readme = ""
        if os.path.isfile(readme_path):
            with open(readme_path) as f:
                readme = f.read()

        with open(readme_path, "w") as f:
            f.write(metadata_section)
            f.write(readme)


def push(
    whl_path,
    namespace: Optional[str] = None,
):
    filename = os.path.basename(whl_path)
    repo_name, version, _, _, _ = filename.split("-")
    versioned_name = repo_name + "-" + version
    repo_local_path = os.path.join("hub", repo_name)

    # Create the repo (or clone its content if it's nonempty)
    api = HfApi()
    repo_url = api.create_repo(
        name=repo_name,
        token=HfFolder.get_token(),
        organization=namespace,
        private=False,
        exist_ok=True,
    )
    repo = Repository(repo_local_path, clone_from=repo_url)
    repo.git_pull(rebase=True)
    repo.lfs_track(["*.whl", "*.npz", "*strings.json", "vectors"])

    # Extract information from whl file
    logger.debug("Extracting information from .whl file.")
    with zipfile.ZipFile(whl_path, "r") as zip_ref:
        base_name = os.path.join(repo_name, versioned_name)
        for file_name in zip_ref.namelist():
            if file_name.startswith(base_name):
                zip_ref.extract(file_name, "hub")

    # Move files up one directory
    extracted_dir = os.path.join(repo_local_path, versioned_name)
    for filename in os.listdir(extracted_dir):
        dst = os.path.join(repo_local_path, filename)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.isfile(dst):
            os.remove(dst)
        shutil.move(os.path.join(extracted_dir, filename), dst)
    shutil.rmtree(os.path.join(repo_local_path, versioned_name))

    # Create model card, including HF tags
    _create_model_card(repo_name, repo_local_path)

    # Remove version from whl filename
    dst_file = os.path.join(repo_local_path, f"{repo_name}-any-py3-none-any.whl")
    shutil.copyfile(whl_path, dst_file)

    logger.debug("Pushing repository to the Hub.")
    url = repo.push_to_hub(commit_message="Spacy Update")
    url, _ = url.split("/commit/")

    logger.info("View your model here: {}".format(url))
    whl_path = f"{url}/resolve/main/{repo_name}-any-py3-none-any.whl"
    logger.info(f"Install your model: pip install {whl_path}")
