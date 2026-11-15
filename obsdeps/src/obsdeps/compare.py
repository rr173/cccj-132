"""Comparison of two explicitly resolved contexts.

Both sides are resolved independently from their own requests.  Nothing on side
B is allowed to influence side A, and a shared parse cache is keyed by
``(repo, observation, path, digest)`` so identical ancestor files may be parsed
once but can never leak values between contexts.

The comparison reports *what changed*, including the case that matters most for
auditing: the final version is identical but the declaration, property, parent
or repository that produced it has changed.
"""

from __future__ import annotations

from typing import Dict, List, Optional



def _dep_index(result: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    out: Dict[str, Dict[str, object]] = {}
    for entry in result.get("dependencies", []):  # type: ignore[union-attr]
        key = entry["key"].get("key") if isinstance(entry.get("key"), dict) else None
        if key is None:
            key = f"?unresolved:{entry['declaration'].get('locator', '')}"
        out.setdefault(str(key), entry)
    return out


def _source_of(entry: Dict[str, object]) -> Dict[str, object]:
    version = entry.get("version", {})
    evidence = version.get("evidence", {}) if isinstance(version, dict) else {}
    return {
        "kind": version.get("source") if isinstance(version, dict) else None,
        "declaration": entry.get("declaration"),
        "declaring_project": evidence.get("declaring_project") or (
            evidence.get("managed_from", {}) or {}
        ).get("repo_id"),
        "managed_from": evidence.get("managed_from") or evidence.get("declared_in"),
        "version_value_source": evidence.get("version_value_source"),
        "properties": evidence.get("properties", []),
        "resolved_version": version.get("resolved") if isinstance(version, dict) else None,
        "raw_version": version.get("raw") if isinstance(version, dict) else None,
    }


def _files_of(entry: Dict[str, object]) -> List[Dict[str, object]]:
    files = entry.get("files", [])
    return [
        {
            "role": f.get("role"),
            "extension": f.get("extension"),
            "classifier": f.get("classifier"),
            "repo_id": f.get("repo_id"),
            "observation_id": f.get("observation_id"),
            "path": f.get("path"),
            "sha256": f.get("sha256"),
            "observed": f.get("observed"),
        }
        for f in files
    ]


def _source_signature(entry: Dict[str, object]) -> Dict[str, object]:
    """Primary evidence: the facts that actually produce the final version.

    Overridden parent declarations are deliberately *not* part of this
    signature.  If a child overrides a management entry, the parent's version
    did not contribute, so a change to that parent file is supporting history,
    not a change in how the final value was derived.  It is still reported, in
    ``supporting_diff``.
    """
    version = entry.get("version", {})
    evidence = version.get("evidence", {}) if isinstance(version, dict) else {}
    return {
        "version_source": version.get("source") if isinstance(version, dict) else None,
        "declaration": entry.get("declaration"),
        "managed_from": evidence.get("managed_from"),
        "version_value_source": evidence.get("version_value_source"),
        "declaring_project": evidence.get("declaring_project"),
        "properties": evidence.get("properties", []),
        "files": _files_of(entry),
    }


def _supporting_signature(entry: Dict[str, object]) -> Dict[str, object]:
    """Evidence that explains the value but did not determine it."""
    version = entry.get("version", {})
    evidence = version.get("evidence", {}) if isinstance(version, dict) else {}
    return {
        "overrides": evidence.get("overrides", []),
        "artifact_gaps": entry.get("artifact_gaps", []),
    }


def compare_results(
    left: Dict[str, object],
    right: Dict[str, object],
) -> Dict[str, object]:
    left_deps = _dep_index(left)
    right_deps = _dep_index(right)
    all_keys = sorted(set(left_deps) | set(right_deps))

    rows: List[Dict[str, object]] = []
    counts = {
        "added": 0,
        "removed": 0,
        "version_changed": 0,
        "source_changed": 0,
        "unchanged": 0,
        "unresolved": 0,
    }
    for key in all_keys:
        a = left_deps.get(key)
        b = right_deps.get(key)
        if a is None:
            classification = "added"
        elif b is None:
            classification = "removed"
        else:
            a_version = (a.get("version") or {}).get("resolved")
            b_version = (b.get("version") or {}).get("resolved")
            a_source = _source_signature(a)
            b_source = _source_signature(b)
            if a_version is None or b_version is None:
                classification = "unresolved" if a_version == b_version else "version_changed"
            elif a_version != b_version:
                classification = "version_changed"
            elif a_source != b_source:
                classification = "source_changed"
            else:
                classification = "unchanged"
        counts[classification] += 1
        row: Dict[str, object] = {
            "key": key,
            "classification": classification,
            "a": _summarise(a) if a is not None else None,
            "b": _summarise(b) if b is not None else None,
        }
        if classification == "source_changed":
            row["source_diff"] = _source_diff(_source_signature(a), _source_signature(b))  # type: ignore[arg-type]
        if a is not None and b is not None:
            supporting = _source_diff(_supporting_signature(a), _supporting_signature(b))  # type: ignore[arg-type]
            if supporting:
                row["supporting_diff"] = supporting
        if classification in ("added", "removed") and (a or b):
            source = a if a is not None else b
            row["source_diff"] = {
                "only_in": "a" if a is not None else "b",
                "source": _source_signature(source),  # type: ignore[arg-type]
            }
        rows.append(row)

    property_diff = _property_diff(left, right)
    management_diff = _management_diff(left, right)
    context_diff = _context_diff(left, right)
    chain_diff = _chain_diff(left, right)

    return {
        "context_a": left.get("context"),
        "context_b": right.get("context"),
        "status_a": left.get("status"),
        "status_b": right.get("status"),
        "summary": counts,
        "context_changes": context_diff,
        "parent_chain_changes": chain_diff,
        "dependency_changes": rows,
        "property_changes": property_diff,
        "management_changes": management_diff,
        "problems": {
            "a": left.get("problems", []),
            "b": right.get("problems", []),
        },
        "missing": {
            "a": left.get("missing", []),
            "b": right.get("missing", []),
        },
        "note": "comparison is between the two explicit requests; later observations "
                "registered in the database do not affect either side",
    }


def _summarise(entry: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    if entry is None:
        return None
    version = entry.get("version", {})
    evidence = version.get("evidence", {}) if isinstance(version, dict) else {}
    return {
        "status": entry.get("status"),
        "version": version.get("resolved") if isinstance(version, dict) else None,
        "raw_version": version.get("raw") if isinstance(version, dict) else None,
        "version_source": version.get("source") if isinstance(version, dict) else None,
        "declared_in": entry.get("declaration"),
        "managed_from": evidence.get("managed_from"),
        "version_value_source": evidence.get("version_value_source"),
        "declaring_project": evidence.get("declaring_project"),
        "files": _files_of(entry),
        "problems": entry.get("problems", []),
        "missing": entry.get("missing", []),
    }


def _source_diff(a: Dict[str, object], b: Dict[str, object]) -> Dict[str, object]:
    diff: Dict[str, object] = {}
    for key in a:
        if a.get(key) != b.get(key):
            diff[key] = {"a": a.get(key), "b": b.get(key)}
    return diff


def _property_diff(left: Dict[str, object], right: Dict[str, object]) -> List[Dict[str, object]]:
    def table(result: Dict[str, object]) -> Dict[str, Dict[str, object]]:
        props = result.get("properties", {})
        effective = props.get("effective", []) if isinstance(props, dict) else []
        return {item["name"]: item for item in effective if item.get("name")}

    a_table = table(left)
    b_table = table(right)
    out: List[Dict[str, object]] = []
    for name in sorted(set(a_table) | set(b_table)):
        a = a_table.get(name)
        b = b_table.get(name)
        a_value = a.get("value") if a else None
        b_value = b.get("value") if b else None
        a_where = (a or {}).get("defined_in")
        b_where = (b or {}).get("defined_in")
        if a_value == b_value and a_where == b_where:
            continue
        out.append({
            "name": name,
            "a": {"value": a_value, "defined_in": a_where, "raw": (a or {}).get("raw")},
            "b": {"value": b_value, "defined_in": b_where, "raw": (b or {}).get("raw")},
            "kind": (
                "definition_moved" if a_value == b_value else
                "added" if a is None else
                "removed" if b is None else
                "value_changed"
            ),
        })
    return out


def _management_diff(left: Dict[str, object], right: Dict[str, object]) -> List[Dict[str, object]]:
    def table(result: Dict[str, object]) -> Dict[str, Dict[str, object]]:
        management = result.get("management", {})
        entries = management.get("entries", []) if isinstance(management, dict) else []
        return {entry["key"]["key"]: entry for entry in entries}

    a_table = table(left)
    b_table = table(right)
    out: List[Dict[str, object]] = []
    for key in sorted(set(a_table) | set(b_table)):
        a = a_table.get(key)
        b = b_table.get(key)
        if a is None or b is None:
            out.append({
                "key": key,
                "kind": "added" if a is None else "removed",
                "a": a, "b": b,
            })
            continue
        a_version = a.get("version")
        b_version = b.get("version")
        a_decl = a.get("declared_in")
        b_decl = b.get("declared_in")
        if a_version == b_version and a_decl == b_decl:
            continue
        out.append({
            "key": key,
            "kind": "version_changed" if a_version != b_version else "definition_moved",
            "a": {"version": a_version, "declared_in": a_decl,
                  "version_value_source": a.get("version_value_source")},
            "b": {"version": b_version, "declared_in": b_decl,
                  "version_value_source": b.get("version_value_source")},
        })
    return out


def _context_diff(left: Dict[str, object], right: Dict[str, object]) -> Dict[str, object]:
    a_context = left.get("context", {}) or {}
    b_context = right.get("context", {}) or {}
    return {
        "chain_changed": a_context.get("chain") != b_context.get("chain"),
        "root_changed": a_context.get("root") != b_context.get("root"),
        "context_sha256": {
            "a": a_context.get("context_sha256"),
            "b": b_context.get("context_sha256"),
        },
        "same_context": a_context.get("context_sha256") == b_context.get("context_sha256"),
    }


def _chain_diff(left: Dict[str, object], right: Dict[str, object]) -> Dict[str, object]:
    a = left.get("parent_chain", [])
    b = right.get("parent_chain", [])
    changes: List[Dict[str, object]] = []
    for index in range(max(len(a), len(b))):
        a_node = a[index] if index < len(a) else None
        b_node = b[index] if index < len(b) else None
        if a_node == b_node:
            continue
        changes.append({"depth": index, "a": a_node, "b": b_node})
    return {
        "changed": bool(changes),
        "changes": changes,
        "a_length": len(a),
        "b_length": len(b),
    }
