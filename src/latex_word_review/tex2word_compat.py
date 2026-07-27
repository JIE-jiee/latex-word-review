"""Narrow, auditable source rewrites for tex2word review exports.

The immutable LaTeX snapshot is never modified.  These helpers operate only on
the derived backend overlay and record hashes and source spans for every
replacement.  The goal is a readable Word review document, not reproduction of
publisher-specific typesetting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from latex_word_review.canonical import sha256_canonical
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes
from latex_word_review.paths import validate_relative_path

_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\s*\{document\}")
_END_DOCUMENT_RE = re.compile(r"\\end\s*\{document\}")
_BODY_START_RE = re.compile(r"\\(?:part|chapter|section)\*?\s*(?:\[[^\]]*\]\s*)?\{")
_FRONT_MATTER_COMMAND_RE = re.compile(
    r"\\(?P<name>"
    r"title|author|address|authormark|titlemark|corres|keywords|abstract|"
    r"editor|presentaddress|fundingInfo|shorttitle|shortauthors|cormark|"
    r"ead|affiliation|cortext"
    r")\b"
)
_KEYWORDS_BEGIN_RE = re.compile(r"\\begin\s*\{keywords\}")
_KEYWORDS_END_RE = re.compile(r"\\end\s*\{keywords\}")
_LAYOUT_CONTROL_RE = re.compile(r"\\(?P<name>captionsetup|linenumbers|nolinenumbers)\b")
_CENTER_BLOCK_RE = re.compile(r"\\begin\s*\{center\}(?P<body>.*?)\\end\s*\{center\}", re.DOTALL)
_CAPTIONOF_RE = re.compile(r"\\captionof\b")
_SUBCAPTIONBOX_RE = re.compile(r"\\subcaptionbox(?=[^A-Za-z@]|$)")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\*)?(?=[^A-Za-z@]|$)")
_LABEL_RE = re.compile(r"\\label\b")
_CENTERING_RE = re.compile(r"\\centering\b")
_STATIC_LABEL_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}\Z")
_ENVIRONMENT_MARKER_RE = re.compile(r"\\(begin|end)\s*\{([A-Za-z*@]+)\}")
_CENTERING_COMMAND_RE = re.compile(r"\\centering(?=[^A-Za-z@]|$)")
_PAR_COMMAND_RE = re.compile(r"\\par(?=[^A-Za-z@]|$)")
_VSPACE_COMMAND_RE = re.compile(r"\\vspace\*?(?=[^A-Za-z@]|$)")
_REFSTEP_COUNTER_RE = re.compile(r"\\refstepcounter(?=[^A-Za-z@]|$)")
_ADD_CONTENTS_LINE_RE = re.compile(r"\\addcontentsline(?=[^A-Za-z@]|$)")
_SMALL_COMMAND_RE = re.compile(r"\\small(?=[^A-Za-z@]|$)")
_CAPTION_STYLE_KEYS = frozenset({"font"})
_CAPTION_STYLE_SELECTORS = frozenset({"table", "figure", "subfigure"})
_WRAPPER_NAMES = frozenset({"orgdiv", "orgname", "orgaddress", "state", "country", "email"})
_DROP_COMMANDS = frozenset({"authormark", "titlemark", "shorttitle", "shortauthors", "cormark"})
_LABELED_COMMANDS = {
    "editor": "Editor",
    "presentaddress": "Present address",
    "fundingInfo": "Funding",
}


@dataclass(frozen=True, slots=True)
class _CommandSpan:
    name: str
    start: int
    end: int
    optional_text: str | None
    argument_text: str
    line: int


@dataclass(frozen=True, slots=True)
class _DerivedRewriteSpan:
    name: str
    start: int
    end: int
    replacement: str
    line: int


def _mask_tex_comments(text: str) -> str:
    characters = list(text)
    in_comment = False
    slash_count = 0
    for index, character in enumerate(characters):
        if in_comment:
            if character in "\r\n":
                in_comment = False
                slash_count = 0
            else:
                characters[index] = " "
            continue
        if character == "%" and slash_count % 2 == 0:
            characters[index] = " "
            in_comment = True
            slash_count = 0
            continue
        if character == "\\":
            slash_count += 1
        else:
            slash_count = 0
    return "".join(characters)


def _skip_space(text: str, position: int, limit: int) -> int:
    while position < limit and text[position].isspace():
        position += 1
    return position


def _balanced_group(
    text: str,
    position: int,
    *,
    opening: str,
    closing: str,
    limit: int,
) -> tuple[int, int] | None:
    if position >= limit or text[position] != opening:
        return None
    depth = 0
    slash_count = 0
    for index in range(position, limit):
        character = text[index]
        escaped = slash_count % 2 == 1
        if character == opening and not escaped:
            depth += 1
        elif character == closing and not escaped:
            depth -= 1
            if depth == 0:
                return position + 1, index
            if depth < 0:
                return None
        if character == "\\":
            slash_count += 1
        else:
            slash_count = 0
    return None


def _scan_front_matter(text: str) -> tuple[_CommandSpan, ...]:
    masked = _mask_tex_comments(text)
    begin = _BEGIN_DOCUMENT_RE.search(masked)
    if begin is None:
        return ()
    body = _BODY_START_RE.search(masked, begin.end())
    limit = len(text) if body is None else body.start()
    commands: list[_CommandSpan] = []
    occupied_until = begin.end()
    for match in _FRONT_MATTER_COMMAND_RE.finditer(masked, begin.end(), limit):
        if match.start() < occupied_until:
            continue
        cursor = _skip_space(masked, match.end(), limit)
        optional: str | None = None
        if cursor < limit and masked[cursor] == "[":
            group = _balanced_group(
                masked,
                cursor,
                opening="[",
                closing="]",
                limit=limit,
            )
            if group is None:
                continue
            optional = text[group[0] : group[1]]
            cursor = _skip_space(masked, group[1] + 1, limit)
        if match.group("name") == "cormark" and (cursor >= limit or masked[cursor] != "{"):
            commands.append(
                _CommandSpan(
                    name=match.group("name"),
                    start=match.start(),
                    end=cursor,
                    optional_text=optional,
                    argument_text="",
                    line=text.count("\n", 0, match.start()) + 1,
                )
            )
            occupied_until = cursor
            continue
        argument = _balanced_group(
            masked,
            cursor,
            opening="{",
            closing="}",
            limit=limit,
        )
        if argument is None:
            continue
        if match.group("name") == "title" and optional is None:
            continue
        end = argument[1] + 1
        commands.append(
            _CommandSpan(
                name=match.group("name"),
                start=match.start(),
                end=end,
                optional_text=optional,
                argument_text=text[argument[0] : argument[1]],
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
        occupied_until = end
    return tuple(commands)


def _scan_keywords_environments(text: str) -> tuple[_CommandSpan, ...]:
    masked = _mask_tex_comments(text)
    begin_document = _BEGIN_DOCUMENT_RE.search(masked)
    if begin_document is None:
        return ()
    body = _BODY_START_RE.search(masked, begin_document.end())
    limit = len(text) if body is None else body.start()
    environments: list[_CommandSpan] = []
    cursor = begin_document.end()
    while begin := _KEYWORDS_BEGIN_RE.search(masked, cursor, limit):
        end = _KEYWORDS_END_RE.search(masked, begin.end(), limit)
        if end is None:
            break
        environments.append(
            _CommandSpan(
                name="keywords_environment",
                start=begin.start(),
                end=end.end(),
                optional_text=None,
                argument_text=text[begin.end() : end.start()],
                line=text.count("\n", 0, begin.start()) + 1,
            )
        )
        cursor = end.end()
    return tuple(environments)


def _flatten_known_wrappers(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            result.append(text[index])
            index += 1
            continue
        name_match = re.match(r"\\([A-Za-z]+)", text[index:])
        if name_match is None or name_match.group(1) not in _WRAPPER_NAMES:
            result.append(text[index])
            index += 1
            continue
        cursor = index + len(name_match.group(0))
        cursor = _skip_space(text, cursor, len(text))
        group = _balanced_group(
            text,
            cursor,
            opening="{",
            closing="}",
            limit=len(text),
        )
        if group is None:
            result.append(text[index])
            index += 1
            continue
        result.append(_flatten_known_wrappers(text[group[0] : group[1]]))
        index = group[1] + 1
    return "".join(result)


def _display_text(text: str) -> str:
    flattened = _flatten_known_wrappers(text)
    masked = _mask_tex_comments(flattened)
    return masked.strip()


def _top_level_items(text: str) -> tuple[str, ...]:
    items: list[str] = []
    start = 0
    depth = 0
    slash_count = 0
    for index, character in enumerate(text):
        escaped = slash_count % 2 == 1
        if character == "{" and not escaped:
            depth += 1
        elif character == "}" and not escaped:
            depth = max(0, depth - 1)
        elif character == "," and not escaped and depth == 0:
            items.append(text[start:index])
            start = index + 1
        if character == "\\":
            slash_count += 1
        else:
            slash_count = 0
    items.append(text[start:])
    return tuple(item.strip() for item in items if item.strip())


def _strip_complete_group(text: str) -> str:
    value = text.strip()
    group = _balanced_group(value, 0, opening="{", closing="}", limit=len(value))
    if group is not None and group[1] == len(value) - 1:
        return value[group[0] : group[1]].strip()
    return value


def _display_structured_affiliation(text: str) -> str:
    known_keys = {"organization", "addressline", "city", "postcode", "country", "state"}
    values: list[str] = []
    for item in _top_level_items(text):
        key, separator, value = item.partition("=")
        if not separator:
            displayed = _display_text(item)
        else:
            displayed_value = _display_text(_strip_complete_group(value))
            normalized_key = key.strip().casefold()
            displayed = (
                displayed_value
                if normalized_key in known_keys
                else f"{key.strip()}: {displayed_value}"
            )
        if displayed:
            values.append(displayed)
    return ", ".join(values)


def _display_keywords(text: str) -> str:
    return re.sub(r"\\sep\b", ";", _display_text(text)).strip(" ;\r\n\t")


def _replacement(command: _CommandSpan) -> str:
    argument = command.argument_text
    if command.name == "title":
        return f"\\title{{{argument}}}"
    if command.name == "author":
        return f"\\author{{{argument}}}"
    if command.name in _DROP_COMMANDS:
        return ""
    if command.name == "address":
        label = "" if command.optional_text is None else f" {command.optional_text.strip()}"
        body = _display_text(argument)
        return "" if not body else f"\n\n\\textit{{Affiliation{label}: {body}}}\n\n"
    if command.name == "corres":
        body = _display_text(argument)
        return "" if not body else f"\n\n\\textit{{Correspondence: {body}}}\n\n"
    if command.name == "keywords":
        body = _display_keywords(argument)
        return "" if not body else f"\n\n\\textbf{{Keywords:}} {body}\n\n"
    if command.name == "keywords_environment":
        body = _display_keywords(argument)
        return "" if not body else f"\n\n\\textbf{{Keywords:}} {body}\n\n"
    if command.name == "abstract":
        return f"\n\\begin{{abstract}}\n{argument}\n\\end{{abstract}}\n"
    if command.name == "ead":
        body = _display_text(argument)
        return "" if not body else f"\n\n\\textit{{Email: {body}}}\n\n"
    if command.name == "affiliation":
        label = "" if command.optional_text is None else f" {command.optional_text.strip()}"
        body = _display_structured_affiliation(argument)
        return "" if not body else f"\n\n\\textit{{Affiliation{label}: {body}}}\n\n"
    if command.name == "cortext":
        body = _display_text(argument)
        return "" if not body else f"\n\n\\textit{{Correspondence: {body}}}\n\n"
    label = _LABELED_COMMANDS[command.name]
    body = _display_text(argument)
    return "" if not body else f"\n\n\\textit{{{label}: {body}}}\n\n"


def rewrite_tex2word_front_matter(
    text: str,
    *,
    source_path: str,
    profile: str,
) -> tuple[str, list[dict[str, object]]]:
    """Normalize publisher front matter in a derived tex2word source tree."""

    validate_relative_path(source_path)
    if not profile:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "compatibility profile is empty")
    result = text
    evidence: list[dict[str, object]] = []
    previous_start = len(text) + 1
    commands = sorted(
        (*_scan_front_matter(text), *_scan_keywords_environments(text)),
        key=lambda command: (command.start, command.end),
    )
    for command in reversed(commands):
        if command.end > previous_start:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "front-matter compatibility spans overlap",
            )
        original = text[command.start : command.end]
        replacement = _replacement(command)
        if result[command.start : command.end] != original:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "front-matter command changed before compatibility rewrite",
            )
        result = result[: command.start] + replacement + result[command.end :]
        start_utf8 = len(text[: command.start].encode("utf-8"))
        end_utf8 = start_utf8 + len(original.encode("utf-8"))
        original_sha256 = digest_bytes(original.encode("utf-8")).sha256
        derived_sha256 = digest_bytes(replacement.encode("utf-8")).sha256
        transformation_id = "compat_" + sha256_canonical(
            {
                "profile": profile,
                "source_path": source_path,
                "start_utf8": start_utf8,
                "end_utf8": end_utf8,
                "original_sha256": original_sha256,
                "derived_sha256": derived_sha256,
            }
        ).removeprefix("sha256:")
        evidence.append(
            {
                "transformation_id": transformation_id,
                "kind": "journal_front_matter_normalization",
                "command": command.name,
                "source_path": source_path,
                "source_span": {
                    "start_char": command.start,
                    "end_char": command.end,
                    "start_utf8": start_utf8,
                    "end_utf8": end_utf8,
                    "line": command.line,
                },
                "optional_argument_removed": command.optional_text is not None,
                "original_block_sha256": original_sha256,
                "derived_block_sha256": derived_sha256,
            }
        )
        previous_start = command.start
    evidence.reverse()
    return result, evidence


def _document_body_bounds(masked: str) -> tuple[int, int] | None:
    begin = _BEGIN_DOCUMENT_RE.search(masked)
    if begin is None:
        return None
    end = _END_DOCUMENT_RE.search(masked, begin.end())
    return begin.end(), len(masked) if end is None else end.start()


def _logical_line_bounds(text: str, position: int, lower: int, upper: int) -> tuple[int, int]:
    previous_newline = text.rfind("\n", lower, position)
    line_start = lower if previous_newline < 0 else previous_newline + 1
    next_newline = text.find("\n", position, upper)
    line_end = upper if next_newline < 0 else next_newline
    return line_start, line_end


def _caption_style_is_safe(text: str) -> bool:
    items = _top_level_items(text)
    if not items:
        return False
    for item in items:
        key, separator, _ = item.partition("=")
        if not separator or key.strip().casefold() not in _CAPTION_STYLE_KEYS:
            return False
    return True


def _scan_layout_controls(text: str) -> tuple[_DerivedRewriteSpan, ...]:
    masked = _mask_tex_comments(text)
    bounds = _document_body_bounds(masked)
    if bounds is None:
        return ()
    lower, upper = bounds
    controls: list[_DerivedRewriteSpan] = []
    occupied_until = lower
    for match in _LAYOUT_CONTROL_RE.finditer(masked, lower, upper):
        if match.start() < occupied_until:
            continue
        line_start, line_end = _logical_line_bounds(masked, match.start(), lower, upper)
        if masked[line_start : match.start()].strip():
            continue
        name = match.group("name")
        end = match.end()
        if name == "captionsetup":
            cursor = _skip_space(masked, end, line_end)
            selector: str | None = None
            if cursor < line_end and masked[cursor] == "[":
                optional = _balanced_group(
                    masked,
                    cursor,
                    opening="[",
                    closing="]",
                    limit=line_end,
                )
                if optional is None:
                    continue
                selector = text[optional[0] : optional[1]].strip().casefold()
                cursor = _skip_space(masked, optional[1] + 1, line_end)
            if selector is not None and selector not in _CAPTION_STYLE_SELECTORS:
                continue
            argument = _balanced_group(
                masked,
                cursor,
                opening="{",
                closing="}",
                limit=line_end,
            )
            if argument is None or not _caption_style_is_safe(text[argument[0] : argument[1]]):
                continue
            end = argument[1] + 1
        if masked[end:line_end].strip():
            continue
        controls.append(
            _DerivedRewriteSpan(
                name=name,
                start=match.start(),
                end=end,
                replacement="",
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
        occupied_until = end
    return tuple(controls)


def _parse_includegraphics(
    masked: str,
    match: re.Match[str],
    limit: int,
) -> int | None:
    cursor = _skip_space(masked, match.end(), limit)
    if cursor < limit and masked[cursor] == "[":
        optional = _balanced_group(
            masked,
            cursor,
            opening="[",
            closing="]",
            limit=limit,
        )
        if optional is None:
            return None
        cursor = _skip_space(masked, optional[1] + 1, limit)
    target = _balanced_group(
        masked,
        cursor,
        opening="{",
        closing="}",
        limit=limit,
    )
    return None if target is None else target[1] + 1


def _parse_required_argument(masked: str, match: re.Match[str], limit: int) -> int | None:
    cursor = _skip_space(masked, match.end(), limit)
    argument = _balanced_group(
        masked,
        cursor,
        opening="{",
        closing="}",
        limit=limit,
    )
    return None if argument is None else argument[1] + 1


def _parse_command_groups(
    masked: str,
    match: re.Match[str],
    *,
    count: int,
    limit: int,
) -> tuple[int, tuple[tuple[int, int], ...]] | None:
    cursor = match.end()
    groups: list[tuple[int, int]] = []
    for _ in range(count):
        cursor = _skip_space(masked, cursor, limit)
        group = _balanced_group(
            masked,
            cursor,
            opening="{",
            closing="}",
            limit=limit,
        )
        if group is None:
            return None
        groups.append(group)
        cursor = group[1] + 1
    return cursor, tuple(groups)


def _blank_relative_span(characters: list[str], start: int, end: int, offset: int) -> None:
    for index in range(start - offset, end - offset):
        characters[index] = " "


def _scan_center_captionof_figures(text: str) -> tuple[_DerivedRewriteSpan, ...]:
    masked = _mask_tex_comments(text)
    bounds = _document_body_bounds(masked)
    if bounds is None:
        return ()
    lower, upper = bounds
    rewrites: list[_DerivedRewriteSpan] = []
    for block in _CENTER_BLOCK_RE.finditer(masked, lower, upper):
        body_start = block.start("body")
        body_end = block.end("body")
        captions = tuple(_CAPTIONOF_RE.finditer(masked, body_start, body_end))
        images = tuple(_INCLUDEGRAPHICS_RE.finditer(masked, body_start, body_end))
        labels = tuple(_LABEL_RE.finditer(masked, body_start, body_end))
        centerings = tuple(_CENTERING_RE.finditer(masked, body_start, body_end))
        if len(captions) != 1 or len(images) != 1 or len(labels) > 1 or len(centerings) > 1:
            continue

        caption = captions[0]
        cursor = _skip_space(masked, caption.end(), body_end)
        kind = _balanced_group(
            masked,
            cursor,
            opening="{",
            closing="}",
            limit=body_end,
        )
        if kind is None or text[kind[0] : kind[1]].strip().casefold() != "figure":
            continue
        cursor = _skip_space(masked, kind[1] + 1, body_end)
        if cursor < body_end and masked[cursor] == "[":
            continue
        content = _balanced_group(
            masked,
            cursor,
            opening="{",
            closing="}",
            limit=body_end,
        )
        image_end = _parse_includegraphics(masked, images[0], body_end)
        label_end = None if not labels else _parse_required_argument(masked, labels[0], body_end)
        if content is None or image_end is None or (labels and label_end is None):
            continue

        recognized: list[tuple[int, int]] = [
            (images[0].start(), image_end),
            (caption.start(), content[1] + 1),
        ]
        if labels:
            assert label_end is not None
            recognized.append((labels[0].start(), label_end))
        recognized.extend((item.start(), item.end()) for item in centerings)
        remaining = list(masked[body_start:body_end])
        for start, end in recognized:
            _blank_relative_span(remaining, start, end, body_start)
        if "".join(remaining).strip():
            continue

        prefix = "\\begin{figure}" if centerings else "\\begin{figure}\n\\centering"
        replacement = (
            prefix
            + text[body_start : caption.start()]
            + f"\\caption{{{text[content[0] : content[1]]}}}"
            + text[content[1] + 1 : body_end]
            + "\\end{figure}"
        )
        rewrites.append(
            _DerivedRewriteSpan(
                name="captionof_figure_center",
                start=block.start(),
                end=block.end(),
                replacement=replacement,
                line=text.count("\n", 0, block.start()) + 1,
            )
        )
    return tuple(rewrites)


def _scan_subcaptionboxes(text: str) -> tuple[_DerivedRewriteSpan, ...]:
    r"""Normalize only static one-image ``subcaptionbox`` commands.

    tex2word 1.0.5 does not register ``\subcaptionbox`` but does understand a
    ``subfigure`` environment. The accepted shape is deliberately narrow: one
    required caption group containing exactly one static ``\label`` and one
    required content group containing exactly one complete
    ``\includegraphics`` command and otherwise only whitespace.
    """

    masked = _mask_tex_comments(text)
    bounds = _document_body_bounds(masked)
    if bounds is None:
        return ()
    lower, upper = bounds
    rewrites: list[_DerivedRewriteSpan] = []
    occupied_until = lower
    environment_events = tuple(_ENVIRONMENT_MARKER_RE.finditer(masked, lower, upper))
    environment_index = 0
    environment_stack: list[str] = []
    for match in _SUBCAPTIONBOX_RE.finditer(masked, lower, upper):
        while (
            environment_index < len(environment_events)
            and environment_events[environment_index].start() < match.start()
        ):
            event = environment_events[environment_index]
            environment_index += 1
            if event.group(1) == "begin":
                environment_stack.append(event.group(2))
            elif not environment_stack or environment_stack.pop() != event.group(2):
                return ()
        if match.start() < occupied_until:
            continue
        if not environment_stack or environment_stack[-1].rstrip("*") != "figure":
            # Never create a nested subfigure or reinterpret a box outside a
            # direct figure body.
            continue
        cursor = _skip_space(masked, match.end(), upper)
        if cursor >= upper or masked[cursor] != "{":
            continue
        caption = _balanced_group(
            masked,
            cursor,
            opening="{",
            closing="}",
            limit=upper,
        )
        if caption is None:
            continue
        cursor = _skip_space(masked, caption[1] + 1, upper)
        if cursor >= upper or masked[cursor] != "{":
            # Optional subcaptionbox arguments are intentionally unsupported.
            continue
        content = _balanced_group(
            masked,
            cursor,
            opening="{",
            closing="}",
            limit=upper,
        )
        if content is None:
            continue

        labels = tuple(_LABEL_RE.finditer(masked, caption[0], caption[1]))
        images = tuple(_INCLUDEGRAPHICS_RE.finditer(masked, content[0], content[1]))
        if len(labels) != 1 or len(images) != 1:
            continue
        label_end = _parse_required_argument(masked, labels[0], caption[1])
        image_end = _parse_includegraphics(masked, images[0], content[1])
        if label_end is None or image_end is None:
            continue

        label_cursor = _skip_space(masked, labels[0].end(), caption[1])
        label_group = _balanced_group(
            masked,
            label_cursor,
            opening="{",
            closing="}",
            limit=caption[1],
        )
        if label_group is None or label_group[1] + 1 != label_end:
            continue
        label = text[label_group[0] : label_group[1]].strip()
        if _STATIC_LABEL_VALUE_RE.fullmatch(label) is None:
            continue

        caption_remaining = list(masked[caption[0] : caption[1]])
        _blank_relative_span(
            caption_remaining,
            labels[0].start(),
            label_end,
            caption[0],
        )
        caption_text = (text[caption[0] : labels[0].start()] + text[label_end : caption[1]]).strip()
        if not caption_text or not "".join(caption_remaining).strip():
            continue

        content_remaining = list(masked[content[0] : content[1]])
        _blank_relative_span(
            content_remaining,
            images[0].start(),
            image_end,
            content[0],
        )
        if "".join(content_remaining).strip():
            continue
        image_text = text[images[0].start() : image_end]
        replacement = (
            "\\begin{subfigure}{\\linewidth}\n"
            "\\centering\n"
            f"{image_text}\n"
            f"\\caption{{{caption_text}}}\n"
            f"\\label{{{label}}}\n"
            "\\end{subfigure}"
        )
        rewrites.append(
            _DerivedRewriteSpan(
                name="subcaptionbox",
                start=match.start(),
                end=content[1] + 1,
                replacement=replacement,
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
        occupied_until = content[1] + 1
    return tuple(rewrites)


def _apply_derived_rewrites(
    text: str,
    spans: tuple[_DerivedRewriteSpan, ...],
    *,
    source_path: str,
    profile: str,
    kind: str = "tex2word_layout_control_normalization",
    span_basis: str = "derived_overlay_after_image_rewrite",
) -> tuple[str, list[dict[str, object]]]:
    result = text
    evidence: list[dict[str, object]] = []
    previous_start = len(text) + 1
    for span in reversed(sorted(spans, key=lambda item: (item.start, item.end))):
        if span.end > previous_start:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "derived compatibility spans overlap",
            )
        original = text[span.start : span.end]
        if result[span.start : span.end] != original:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "derived command changed before compatibility rewrite",
            )
        result = result[: span.start] + span.replacement + result[span.end :]
        start_utf8 = len(text[: span.start].encode("utf-8"))
        end_utf8 = start_utf8 + len(original.encode("utf-8"))
        original_sha256 = digest_bytes(original.encode("utf-8")).sha256
        derived_sha256 = digest_bytes(span.replacement.encode("utf-8")).sha256
        transformation_id = "compat_" + sha256_canonical(
            {
                "profile": profile,
                "source_path": source_path,
                "span_basis": span_basis,
                "kind": kind,
                "command": span.name,
                "start_utf8": start_utf8,
                "end_utf8": end_utf8,
                "original_sha256": original_sha256,
                "derived_sha256": derived_sha256,
            }
        ).removeprefix("sha256:")
        evidence.append(
            {
                "transformation_id": transformation_id,
                "kind": kind,
                "command": span.name,
                "source_path": source_path,
                "span_basis": span_basis,
                "derived_span": {
                    "start_char": span.start,
                    "end_char": span.end,
                    "start_utf8": start_utf8,
                    "end_utf8": end_utf8,
                    "line": span.line,
                },
                "original_block_sha256": original_sha256,
                "derived_block_sha256": derived_sha256,
            }
        )
        previous_start = span.start
    evidence.reverse()
    return result, evidence


def rewrite_tex2word_subcaptionboxes(
    text: str,
    *,
    source_path: str,
    profile: str,
) -> tuple[str, list[dict[str, object]]]:
    """Expose static one-image ``subcaptionbox`` content to tex2word."""

    validate_relative_path(source_path)
    if not profile:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "compatibility profile is empty")
    return _apply_derived_rewrites(
        text,
        _scan_subcaptionboxes(text),
        source_path=source_path,
        profile=profile,
        kind="figure_subcaptionbox_normalization",
    )


def normalize_tex2word_manual_figure_minipage_body(
    text: str,
) -> tuple[str, str, str] | None:
    """Normalize one exact, independently numbered minipage figure body.

    The bounded shape is ``includegraphics`` followed by ``par``/``vspace``,
    ``refstepcounter{figure}``, one static label, a ``Fig. thefigure`` display,
    a small caption, and one ``addcontentsline{lof}{figure}``. Any additional
    content, optional construct, comment, or malformed group is left untouched.
    """

    masked = _mask_tex_comments(text)
    if masked != text:
        return None
    limit = len(text)
    cursor = _skip_space(masked, 0, limit)
    if cursor < limit and masked[cursor] == "[":
        placement = _balanced_group(
            masked,
            cursor,
            opening="[",
            closing="]",
            limit=limit,
        )
        if placement is None:
            return None
        cursor = _skip_space(masked, placement[1] + 1, limit)
    width = _balanced_group(
        masked,
        cursor,
        opening="{",
        closing="}",
        limit=limit,
    )
    if width is None or not masked[width[0] : width[1]].strip():
        return None
    if width[1] - width[0] > 256:
        return None
    cursor = _skip_space(masked, width[1] + 1, limit)
    centering = _CENTERING_COMMAND_RE.match(masked, cursor, limit)
    if centering is None:
        return None
    cursor = _skip_space(masked, centering.end(), limit)

    image = _INCLUDEGRAPHICS_RE.match(masked, cursor, limit)
    if image is None:
        return None
    image_end = _parse_includegraphics(masked, image, limit)
    if image_end is None:
        return None
    cursor = _skip_space(masked, image_end, limit)
    paragraph = _PAR_COMMAND_RE.match(masked, cursor, limit)
    if paragraph is None:
        return None
    cursor = _skip_space(masked, paragraph.end(), limit)
    vspace = _VSPACE_COMMAND_RE.match(masked, cursor, limit)
    if vspace is None:
        return None
    parsed = _parse_command_groups(masked, vspace, count=1, limit=limit)
    if parsed is None or not masked[parsed[1][0][0] : parsed[1][0][1]].strip():
        return None
    cursor = _skip_space(masked, parsed[0], limit)

    refstep = _REFSTEP_COUNTER_RE.match(masked, cursor, limit)
    if refstep is None:
        return None
    parsed = _parse_command_groups(masked, refstep, count=1, limit=limit)
    if parsed is None or masked[parsed[1][0][0] : parsed[1][0][1]].strip() != "figure":
        return None
    cursor = _skip_space(masked, parsed[0], limit)
    label_match = _LABEL_RE.match(masked, cursor, limit)
    if label_match is None:
        return None
    parsed = _parse_command_groups(masked, label_match, count=1, limit=limit)
    if parsed is None:
        return None
    label = text[parsed[1][0][0] : parsed[1][0][1]].strip()
    if _STATIC_LABEL_VALUE_RE.fullmatch(label) is None:
        return None
    cursor = _skip_space(masked, parsed[0], limit)

    display = _balanced_group(
        masked,
        cursor,
        opening="{",
        closing="}",
        limit=limit,
    )
    if display is None:
        return None
    display_text = re.sub(r"\s+", "", masked[display[0] : display[1]])
    if display_text != r"\small\bfseriesFig.~\thefigure:":
        return None
    cursor = _skip_space(masked, display[1] + 1, limit)
    caption_group = _balanced_group(
        masked,
        cursor,
        opening="{",
        closing="}",
        limit=limit,
    )
    if caption_group is None:
        return None
    caption_cursor = _skip_space(masked, caption_group[0], caption_group[1])
    small = _SMALL_COMMAND_RE.match(masked, caption_cursor, caption_group[1])
    if small is None:
        return None
    caption = text[small.end() : caption_group[1]].strip()
    if not caption:
        return None
    cursor = _skip_space(masked, caption_group[1] + 1, limit)
    paragraph = _PAR_COMMAND_RE.match(masked, cursor, limit)
    if paragraph is None:
        return None
    cursor = _skip_space(masked, paragraph.end(), limit)

    add_contents = _ADD_CONTENTS_LINE_RE.match(masked, cursor, limit)
    if add_contents is None:
        return None
    parsed = _parse_command_groups(masked, add_contents, count=3, limit=limit)
    if parsed is None:
        return None
    groups = parsed[1]
    if masked[groups[0][0] : groups[0][1]].strip() != "lof":
        return None
    if masked[groups[1][0] : groups[1][1]].strip() != "figure":
        return None
    list_entry = re.sub(r"\s+", "", masked[groups[2][0] : groups[2][1]])
    if not list_entry.startswith(r"\protect\numberline{\thefigure}"):
        return None
    if list_entry == r"\protect\numberline{\thefigure}":
        return None
    if _skip_space(masked, parsed[0], limit) != limit:
        return None

    normalized = (
        text[:image_end].rstrip() + "\n" + f"\\caption{{{caption}}}\n" + f"\\label{{{label}}}\n"
    )
    return normalized, label, caption


def rewrite_tex2word_layout_controls(
    text: str,
    *,
    source_path: str,
    profile: str,
) -> tuple[str, list[dict[str, object]]]:
    """Remove layout-only controls and normalize one safe ``captionof`` shape."""

    validate_relative_path(source_path)
    if not profile:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "compatibility profile is empty")
    rewritten, caption_evidence = _apply_derived_rewrites(
        text,
        _scan_center_captionof_figures(text),
        source_path=source_path,
        profile=profile,
    )
    rewritten, control_evidence = _apply_derived_rewrites(
        rewritten,
        _scan_layout_controls(rewritten),
        source_path=source_path,
        profile=profile,
    )
    return rewritten, caption_evidence + control_evidence
