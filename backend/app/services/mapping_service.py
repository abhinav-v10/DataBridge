import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.mapping import SchemaSide, compare_schemas, side_from_schema, validate_pairs
from app.mapping.matcher import Issue, SchemaComparison, ValidationReport
from app.models.mapping import SchemaMapping
from app.schemas.mapping import ComparisonRequest, MappingCreate
from app.services import connection_service as conn_svc

logger = logging.getLogger("databridge.mapping")


class MappingNotFound(Exception):
    pass


class DuplicateMapping(Exception):
    pass


class MappingInvalid(Exception):
    """The requested mapping failed validation. `.report` holds the details."""

    def __init__(self, message: str, report: ValidationReport | None = None):
        super().__init__(message)
        self.report = report


def load_side(db: Session, connection_id: int, table: str) -> SchemaSide:
    """Reflect one table. Raises ConnectionNotFound / ConnectorError (messages are user-presentable)."""
    connector = conn_svc.connector_for(conn_svc.get_connection(db, connection_id))
    try:
        return side_from_schema(connector.get_table_schema(table), connector.db_type)
    finally:
        connector.dispose()


def compare(db: Session, req: ComparisonRequest) -> SchemaComparison:
    source = load_side(db, req.source_connection_id, req.source_table)
    target = load_side(db, req.target_connection_id, req.target_table)
    return compare_schemas(source, target, req.min_confidence)


def create_mapping(db: Session, data: MappingCreate) -> SchemaMapping:
    if db.scalar(select(SchemaMapping).where(SchemaMapping.name == data.name)):
        raise DuplicateMapping(f"A mapping named '{data.name}' already exists")
    source = load_side(db, data.source_connection_id, data.source_table)
    target = load_side(db, data.target_connection_id, data.target_table)

    if data.columns is None:
        cmp = compare_schemas(source, target, data.min_confidence)
        accepted = [m for m in cmp.mappings if m.status != "conflict"]
        if not accepted:
            raise MappingInvalid("No columns could be mapped automatically; supply 'columns' explicitly")
        pairs = [(m.source_column, m.target_column) for m in accepted]
        saved = [{"source_column": m.source_column, "target_column": m.target_column,
                  "origin": "auto", "confidence": m.confidence} for m in accepted]
    else:
        pairs = [(c.source_column, c.target_column) for c in data.columns]
        if not pairs:
            raise MappingInvalid("'columns' must contain at least one column pair")
        saved = [{"source_column": s, "target_column": t, "origin": "manual", "confidence": None} for s, t in pairs]

    report = validate_pairs(source, target, pairs, data.key_columns)
    if not report.valid:
        raise MappingInvalid("Mapping failed validation", report)

    m = SchemaMapping(
        name=data.name,
        source_connection_id=data.source_connection_id,
        target_connection_id=data.target_connection_id,
        source_table=data.source_table,
        target_table=data.target_table,
        key_columns=list(data.key_columns),
        config={"columns": saved, "min_confidence": data.min_confidence if data.columns is None else None},
    )
    db.add(m)
    db.commit()
    logger.info("mapping %s created (%s -> %s)", m.id, data.source_table, data.target_table)
    return m


def get_mapping(db: Session, mapping_id: int) -> SchemaMapping:
    m = db.get(SchemaMapping, mapping_id)
    if m is None:
        raise MappingNotFound(f"Mapping {mapping_id} not found")
    return m


def list_mappings(db: Session) -> list[SchemaMapping]:
    return list(db.scalars(select(SchemaMapping).order_by(SchemaMapping.id)))


def delete_mapping(db: Session, mapping_id: int) -> None:
    db.delete(get_mapping(db, mapping_id))
    db.commit()


def validate_mapping(db: Session, mapping_id: int) -> ValidationReport:
    """Re-validate a saved mapping against the *current* live schemas (they may have drifted)."""
    m = get_mapping(db, mapping_id)
    errors: list[Issue] = []
    sides = []
    for role, cid, table in (("source", m.source_connection_id, m.source_table),
                             ("target", m.target_connection_id, m.target_table)):
        try:
            sides.append(load_side(db, cid, table))
        except ConnectorError as e:
            if "does not exist" in str(e):
                errors.append(Issue("error", f"{role}_table_missing", str(e)))
                sides.append(None)
            else:
                raise
    if errors:
        return ValidationReport(errors, [], [])
    pairs = [(c["source_column"], c["target_column"]) for c in m.config["columns"]]
    return validate_pairs(sides[0], sides[1], pairs, m.key_columns)
