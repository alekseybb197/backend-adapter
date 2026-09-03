"""artifact_tree_registry — класс ArtifactRegistry для хранения артефактов."""

import os

from .artifact_tree_common import (
    YAML_AVAILABLE,
    logger,
    normalize_for_dedup,
    sha12,
    strip_trailing_line_whitespace,
)


class ArtifactRegistry:
    def __init__(self):
        self.by_hash = {}  # sha(normalized) -> {domain, name, first_part_id, text}
        self._name_counts = {}  # base_name -> counter
        self.protocol_id_to_artifact = {}  # protocol_id -> artifact_name

    def register(self, domain: str, text: str, part_id: str) -> str:
        text = strip_trailing_line_whitespace(text)
        h = sha12(normalize_for_dedup(text))
        if h in self.by_hash:
            return self.by_hash[h]["name"]
        base_name = f"{domain}-{part_id}"
        name = base_name
        if self._name_counts.get(base_name, 0) > 0:
            name = f"{base_name}-{self._name_counts[base_name]}"
        self._name_counts[base_name] = self._name_counts.get(base_name, 0) + 1
        self.by_hash[h] = {"domain": domain, "name": name, "first_part_id": part_id, "text": text}
        return name

    def name_for(self, text: str):
        return self.by_hash.get(sha12(normalize_for_dedup(text)), {}).get("name")

    def link_protocol_id(self, protocol_id: str, artifact_name: str):
        """Запоминает, что 'сырой' id вызова инструмента соответствует артефакту toolcall."""
        if protocol_id:
            self.protocol_id_to_artifact[protocol_id] = artifact_name

    def artifact_for_protocol_id(self, protocol_id: str):
        return self.protocol_id_to_artifact.get(protocol_id)

    def write_all(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)
        if not YAML_AVAILABLE:
            logger.warning("PyYAML не установлен — артефакты сохранены как .txt вместо .yaml.")
        for entry in self.by_hash.values():
            if YAML_AVAILABLE:
                path = os.path.join(out_dir, f"{entry['name']}.yaml")
                data = {
                    "domain": entry["domain"],
                    "first_seen_part_id": entry["first_part_id"],
                    "sha256_raw": sha12(entry["text"]),
                    "sha256_normalized": sha12(normalize_for_dedup(entry["text"])),
                    "content": entry["text"],
                }
                import yaml

                with open(path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(
                        data,
                        f,
                        allow_unicode=True,
                        sort_keys=False,
                        default_flow_style=False,
                        width=100000,
                    )
            else:
                path = os.path.join(out_dir, f"{entry['name']}.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"# domain: {entry['domain']}\n")
                    f.write(f"# first_seen_part_id: {entry['first_part_id']}\n")
                    f.write(f"# sha256[:12] (raw): {sha12(entry['text'])}\n")
                    f.write(
                        f"# sha256[:12] (normalized, use for dedup identity): "
                        f"{sha12(normalize_for_dedup(entry['text']))}\n"
                    )
                    f.write("# ---\n")
                    f.write(entry["text"])
