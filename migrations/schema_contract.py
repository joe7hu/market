"""Compact catalog contract for adopting the pre-baseline database safely."""

from __future__ import annotations

import hashlib
import json


SCHEMAS = ("catalog", "ingest", "raw", "analysis", "app", "ops")


def schema_contract(connection) -> dict[str, str]:
    """Hash definitions, never data; managed partition children are independent."""
    queries = {
        "table_rule": """SELECT n.nspname||'.'||c.relname||'.'||r.rulename,
                jsonb_build_array(pg_get_ruledef(r.oid),r.ev_enabled)
            FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p')""",
        "unsafe_partition": """SELECT n.nspname||'.'||c.relname,
                jsonb_build_array(pg_get_userbyid(c.relowner),c.relacl,c.relrowsecurity,c.relforcerowsecurity)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND c.relispartition AND c.relkind IN ('r','p')
              AND (c.relowner<>(SELECT oid FROM pg_roles WHERE rolname=current_user)
                OR c.relrowsecurity OR c.relforcerowsecurity
                OR EXISTS (SELECT 1 FROM aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
                           WHERE a.grantee<>c.relowner OR a.grantor<>c.relowner)
                OR EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid
                           AND cardinality(a.attacl)>0))""",
        "unsafe_partition_trigger": """SELECT n.nspname||'.'||c.relname||'.'||t.tgname,
                jsonb_build_array(t.tgenabled,t.tgtype,t.tgfoid::regproc::text)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_trigger parent ON parent.oid=t.tgparentid
            WHERE n.nspname=ANY(%s) AND c.relispartition AND NOT t.tgisinternal
              AND (parent.oid IS NULL OR t.tgenabled<>parent.tgenabled
                   OR t.tgtype<>parent.tgtype OR t.tgfoid<>parent.tgfoid)""",
        # Managed partition names vary, but every constraint trigger must enforce
        # ordinary writes. Record only violations so new healthy children are safe.
        "disabled_constraint_trigger": """SELECT n.nspname||'.'||c.relname||':'||
                cn.nspname||'.'||ct.relname||'.'||co.conname||':'||t.tgtype::text,
                t.tgenabled
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_constraint co ON co.oid=t.tgconstraint
            JOIN pg_class ct ON ct.oid=co.conrelid
            JOIN pg_namespace cn ON cn.oid=ct.relnamespace
            WHERE n.nspname=ANY(%s) AND t.tgisinternal AND t.tgenabled<>'O'""",
        "sequence": """SELECT n.nspname||'.'||c.relname,
            jsonb_build_array(format_type(s.seqtypid,NULL),s.seqstart,s.seqincrement,
                s.seqmax,s.seqmin,s.seqcache,s.seqcycle,
                (SELECT jsonb_agg(jsonb_build_array(tn.nspname,t.relname,a.attname,d.deptype)
                    ORDER BY tn.nspname,t.relname,a.attname,d.deptype)
                 FROM pg_depend d JOIN pg_class t ON t.oid=d.refobjid
                 JOIN pg_namespace tn ON tn.oid=t.relnamespace
                 LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
                 WHERE d.classid='pg_class'::regclass AND d.objid=c.oid
                   AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')))
            FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)""",
        "relation": """SELECT n.nspname||'.'||c.relname,
            jsonb_build_array(c.relkind,c.relrowsecurity,c.relforcerowsecurity,
                r.rolname,
                CASE WHEN c.relkind='p' THEN pg_get_partkeydef(c.oid) END)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_roles r ON r.oid=c.relowner
            WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m','S')
                AND NOT c.relispartition""",
        "column": """SELECT n.nspname||'.'||c.relname||'.'||a.attname,
            jsonb_build_array(format_type(a.atttypid,a.atttypmod),a.attnotnull,
                a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid))
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m')
                AND NOT c.relispartition AND a.attnum>0 AND NOT a.attisdropped""",
        "constraint": """SELECT n.nspname||'.'||c.relname||'.'||co.conname,
            jsonb_build_array(pg_get_constraintdef(co.oid),co.convalidated)
            FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND NOT c.relispartition AND co.conparentid=0""",
        "index": """SELECT n.nspname||'.'||i.relname,
            jsonb_build_array(pg_get_indexdef(x.indexrelid),x.indisvalid,x.indisready)
            FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
            JOIN pg_class c ON c.oid=x.indrelid
            JOIN pg_namespace n ON n.oid=i.relnamespace
            WHERE n.nspname=ANY(%s) AND NOT c.relispartition""",
        "function": """SELECT n.nspname||'.'||p.proname||'('||
                pg_get_function_identity_arguments(p.oid)||')',
            jsonb_build_array(pg_get_functiondef(p.oid),
                r.rolname)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_roles r ON r.oid=p.proowner
            WHERE n.nspname=ANY(%s) AND p.prokind IN ('f','p')""",
        "trigger": """SELECT n.nspname||'.'||c.relname||'.'||t.tgname,
            jsonb_build_array(pg_get_triggerdef(t.oid),t.tgenabled)
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND NOT t.tgisinternal AND NOT c.relispartition""",
        "view": """SELECT n.nspname||'.'||c.relname,pg_get_viewdef(c.oid)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND c.relkind IN ('v','m')""",
        "schema_acl": """SELECT n.nspname,jsonb_build_array(pg_get_userbyid(n.nspowner),
            (SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(a.grantor),
                CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                a.privilege_type,a.is_grantable))
             FROM aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a))
            FROM pg_namespace n WHERE n.nspname=ANY(%s) OR n.nspname='public'""",
        "table_acl": """SELECT n.nspname||'.'||c.relname,
            (SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(a.grantor),
                CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                a.privilege_type,a.is_grantable))
             FROM aclexplode(coalesce(c.relacl,acldefault(
                CASE WHEN c.relkind='S' THEN 'S' ELSE 'r' END::\"char\",c.relowner))) a)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE (n.nspname=ANY(%s) OR (n.nspname='public' AND c.relname='alembic_version'))
                AND c.relkind IN ('r','p','v','m','S') AND NOT c.relispartition""",
        "column_acl": """SELECT n.nspname||'.'||c.relname||'.'||at.attname,
            (SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(a.grantor),
                CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                a.privilege_type,a.is_grantable)) FROM aclexplode(at.attacl) a)
            FROM pg_attribute at JOIN pg_class c ON c.oid=at.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=ANY(%s) AND NOT c.relispartition AND at.attacl IS NOT NULL""",
        "function_acl": """SELECT n.nspname||'.'||p.proname||'('||
                pg_get_function_identity_arguments(p.oid)||')',
            (SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(a.grantor),
                CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                a.privilege_type,a.is_grantable))
             FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s)""",
        "policy": """SELECT n.nspname||'.'||c.relname||'.'||p.polname,
            jsonb_build_array(p.polcmd,p.polpermissive,p.polroles,
                pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid))
            FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)""",
    }
    owner = connection.execute("SELECT current_user").fetchone()[0]
    result = {}
    for kind, query in queries.items():
        for name, definition in connection.execute(query, [list(SCHEMAS)]).fetchall():
            if kind == "relation" and definition[3] == owner:
                definition[3] = "migration_owner"
            elif kind == "function" and definition[1] == owner:
                definition[1] = "migration_owner"
            elif kind.endswith("_acl"):
                schema_owner = None
                if kind == "schema_acl":
                    schema_owner, definition = definition
                definition = sorted(
                    [["migration_owner" if value == owner else value for value in grant] for grant in definition or []],
                    key=lambda grant: json.dumps(grant),
                )
                if schema_owner is not None:
                    definition = ["migration_owner" if schema_owner == owner else schema_owner, definition]
            encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
            result[f"{kind}:{name}"] = hashlib.sha256(encoded).hexdigest()
    return result


def schema_hash(connection) -> str:
    return hashlib.sha256(json.dumps(schema_contract(connection), sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()
