PRAGMA foreign_keys = ON;

CREATE TABLE catalogue_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE editions (
    set_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    set_type TEXT NOT NULL,
    released_at TEXT,
    digital INTEGER NOT NULL,
    source_card_count INTEGER,
    eligibility TEXT NOT NULL,
    exclusion_reason TEXT
) WITHOUT ROWID;
CREATE INDEX editions_code_idx ON editions(code);
CREATE INDEX editions_release_idx ON editions(released_at, code);

CREATE TABLE oracle_cards (
    oracle_id TEXT PRIMARY KEY,
    source_record_count INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE printings (
    printing_id TEXT PRIMARY KEY,
    set_id TEXT NOT NULL REFERENCES editions(set_id),
    collector_number TEXT NOT NULL,
    oracle_id TEXT REFERENCES oracle_cards(oracle_id),
    layout TEXT NOT NULL,
    illustration_id TEXT,
    identity_status TEXT NOT NULL CHECK(identity_status IN ('confirmed_multilang','single_language','unresolved_identity')),
    identity_evidence TEXT NOT NULL,
    variant_attributes_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX printings_set_number_idx ON printings(set_id, collector_number, printing_id);
CREATE INDEX printings_oracle_idx ON printings(oracle_id);
CREATE INDEX printings_layout_idx ON printings(layout);

CREATE TABLE printing_added (
    printing_id TEXT PRIMARY KEY REFERENCES printings(printing_id),
    added_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE source_records (
    provider_card_id TEXT PRIMARY KEY,
    printing_id TEXT NOT NULL REFERENCES printings(printing_id),
    lang TEXT NOT NULL,
    source_uri TEXT,
    name TEXT NOT NULL,
    printed_name TEXT,
    type_line TEXT,
    printed_type_line TEXT,
    mana_cost TEXT,
    mana_value REAL,
    colors_json TEXT NOT NULL,
    color_identity_json TEXT NOT NULL,
    colors_key TEXT NOT NULL,
    color_identity_key TEXT NOT NULL,
    rarity TEXT,
    artist_json TEXT NOT NULL,
    oracle_text TEXT,
    printed_text TEXT,
    raw_json TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX source_records_lang_name_idx ON source_records(lang, name COLLATE NOCASE);
CREATE INDEX source_records_type_idx ON source_records(type_line COLLATE NOCASE);
CREATE INDEX source_records_rarity_idx ON source_records(rarity);
CREATE INDEX source_records_mana_idx ON source_records(mana_value);
CREATE INDEX source_records_colors_idx ON source_records(colors_key);
CREATE INDEX source_records_color_identity_idx ON source_records(color_identity_key);
CREATE INDEX source_records_mana_cost_idx ON source_records(mana_cost);
CREATE INDEX source_records_printing_idx ON source_records(printing_id,lang,provider_card_id);

CREATE TABLE printing_languages (
    printing_id TEXT NOT NULL REFERENCES printings(printing_id),
    lang TEXT NOT NULL,
    provider_card_id TEXT NOT NULL UNIQUE REFERENCES source_records(provider_card_id),
    PRIMARY KEY(printing_id, lang)
) WITHOUT ROWID;

CREATE TABLE card_faces (
    printing_id TEXT NOT NULL REFERENCES printings(printing_id),
    face_index INTEGER NOT NULL CHECK(face_index >= 0),
    physical_image INTEGER NOT NULL CHECK(physical_image IN (0,1)),
    PRIMARY KEY(printing_id, face_index)
) WITHOUT ROWID;

CREATE TABLE image_assets (
    asset_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    width INTEGER NOT NULL CHECK(width > 0),
    height INTEGER NOT NULL CHECK(height > 0),
    language TEXT NOT NULL,
    provenance_json TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE image_slots (
    printing_id TEXT NOT NULL,
    face_index INTEGER NOT NULL,
    preferred_language TEXT NOT NULL,
    selected_language TEXT,
    status TEXT NOT NULL CHECK(status IN ('missing','pending_review','ready')),
    asset_id TEXT REFERENCES image_assets(asset_id),
    provenance_json TEXT NOT NULL,
    PRIMARY KEY(printing_id, face_index),
    FOREIGN KEY(printing_id, face_index) REFERENCES card_faces(printing_id, face_index),
    CHECK((status='ready' AND asset_id IS NOT NULL) OR (status!='ready' AND asset_id IS NULL))
) WITHOUT ROWID;

CREATE TABLE identity_exceptions (
    provider_card_id TEXT PRIMARY KEY REFERENCES source_records(provider_card_id),
    exception_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    provenance_json TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE legacy_aliases (
    legacy_id TEXT PRIMARY KEY,
    v2_printing_id TEXT REFERENCES printings(printing_id),
    evidence TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed','ambiguous','unmatched'))
) WITHOUT ROWID;

CREATE VIRTUAL TABLE source_search USING fts5(
    provider_card_id UNINDEXED,
    set_code,
    lang,
    name,
    printed_name,
    type_line,
    printed_type_line,
    oracle_text,
    printed_text
);
