import os
from django.db import connections, transaction

GUAC_RDP_HOST = os.environ.get("GUAC_RDP_HOST", "10.50.0.2")
GUAC_RDP_PORT = os.environ.get("GUAC_RDP_PORT", "3389")
GUAC_ADMIN_ENTITY_ID = int(os.environ.get("GUAC_ADMIN_ENTITY_ID", "2"))

def ensure_rdp_connection(name: str, win_user: str, win_pass: str) -> int:
    with connections["guacamole"].cursor() as cur:
        with transaction.atomic(using="guacamole"):
            cur.execute(
                """
                INSERT INTO guacamole_connection
                  (connection_name, parent_id, protocol, max_connections, max_connections_per_user, connection_weight, failover_only)
                VALUES (%s, NULL, 'rdp', 0, 0, NULL, FALSE)
                RETURNING connection_id
                """,
                [name],
            )
            cid = cur.fetchone()[0]

            params = {
                "hostname": GUAC_RDP_HOST,
                "port": str(GUAC_RDP_PORT),
                "username": win_user,
                "password": win_pass,
                "security": "nla",
                "ignore-cert": "true",
                "enable-drive": "false",
                "enable-printing": "false",
                "enable-audio": "false",
                "enable-wallpaper": "false",
            }
            for k, v in params.items():
                cur.execute(
                    """
                    INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
                    VALUES (%s, %s, %s)
                    """,
                    [cid, k, v],
                )

            for perm in ("READ", "UPDATE", "DELETE", "ADMINISTER"):
                cur.execute(
                    """
                    INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
                    VALUES (%s, %s, %s::guacamole_object_permission_type)
                    ON CONFLICT DO NOTHING
                    """,
                    [GUAC_ADMIN_ENTITY_ID, cid, perm],
                )
            return cid
