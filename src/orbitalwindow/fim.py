# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Code completion (fill-in-the-middle, FIM) support of a model (#79).

Editors such as Zed and Continue complete code with a prompt made of the code before and after the
cursor between the model's FIM tokens, sent to ``/v1/completions``. A model can do that only when
its vocabulary has those tokens; and it does it well only when it was trained for it (a *coder*
model). So there are three states:

- ``coder``: FIM tokens and a coder model (``coder``, ``codegemma``, ``codellama``, ``codestral`` in
  the name);
- ``tokens``: FIM tokens, but a general model (it works, the completions may be poor);
- ``none``: no FIM tokens.

An admin can set the state of a model by hand (setting ``fim.<repo>``).
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import gguf

log = logging.getLogger(__name__)

STATES = ("coder", "tokens", "none")
# FIM tokens (prefix, suffix, middle) by family; the editors build the prompt from these.
FAMILIES: dict[str, tuple[str, str, str]] = {
    "qwen": ("<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>"),  # also CodeGemma
    "starcoder": ("<fim_prefix>", "<fim_suffix>", "<fim_middle>"),
    "deepseek": ("<｜fim▁begin｜>", "<｜fim▁hole｜>", "<｜fim▁end｜>"),
    "codellama": ("▁<PRE>", "▁<SUF>", "▁<MID>"),
}
CODER_RE = re.compile(r"cod(er|egemma|ellama|estral)", re.IGNORECASE)


@dataclass(frozen=True)
class FimSupport:
    state: str  # coder | tokens | none
    family: str = ""  # a key of FAMILIES, empty without FIM tokens
    manual: bool = False  # set by an admin, not found in the files

    @property
    def tokens(self) -> tuple[str, str, str] | None:
        """Return the family's FIM tokens (prefix, suffix, middle).

        Returns:
            The tokens, or None without a family.
        """
        return FAMILIES.get(self.family)


def family_of(vocabulary: set[str]) -> str:
    """Return the FIM family whose three tokens are all in a vocabulary.

    Args:
        vocabulary: Token strings.

    Returns:
        The family, or an empty string.
    """
    for family, tokens in FAMILIES.items():
        if all(token in vocabulary for token in tokens):
            return family
    return ""


def gguf_vocabulary(path: Path) -> set[str]:
    """Read the token strings of a GGUF file.

    Args:
        path: The GGUF file (the first part of a split model).

    Returns:
        The tokens; empty when the file can't be read.
    """
    try:
        with open(path, "rb") as file:
            metadata = gguf.read_metadata(
                file.read(4 << 20),
                lambda offset: gguf._read_at(file, offset),
                lambda key: key == "tokenizer.ggml.tokens",
            )
    except (OSError, gguf.GgufError) as exc:
        log.info("could not read the vocabulary of %s: %s", path, exc)
        return set()
    return set(metadata.get("tokenizer.ggml.tokens") or ())


def hf_vocabulary(folder: Path) -> set[str]:
    """Read the added (special) tokens of a Hugging Face model.

    Args:
        folder: The model folder.

    Returns:
        The tokens from ``tokenizer_config.json`` (``added_tokens_decoder``) and
        ``tokenizer.json`` (``added_tokens``); empty when neither can be read.
    """
    found: set[str] = set()
    try:
        config = json.loads((folder / "tokenizer_config.json").read_text())
        found |= {row.get("content", "") for row in config.get("added_tokens_decoder", {}).values()}
    except OSError, ValueError, AttributeError:
        pass
    try:
        tokenizer = json.loads((folder / "tokenizer.json").read_text())
        found |= {row.get("content", "") for row in tokenizer.get("added_tokens", [])}
    except OSError, ValueError, AttributeError:
        pass
    return found


def detect(repo: str, vocabulary: set[str]) -> FimSupport:
    """Decide a model's FIM support from its name and vocabulary.

    Args:
        repo: The model id.
        vocabulary: Its tokens (see ``gguf_vocabulary`` and ``hf_vocabulary``).

    Returns:
        The support.
    """
    family = family_of(vocabulary)
    if not family:
        return FimSupport("none")
    return FimSupport("coder" if CODER_RE.search(repo) else "tokens", family)
