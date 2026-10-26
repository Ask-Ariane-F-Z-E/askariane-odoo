-- A database copied from production (an Odoo.sh staging or dev branch, a
-- restore) must not hold the Ask Ariane connection: with its private key it
-- could push as production. Wipe every askariane_connect.* parameter (key,
-- pending key, key id, generation, tenant, pinned origin, companies, expiry,
-- account) and every connect attempt, finished or not.
DELETE FROM ir_config_parameter
 WHERE left(key, 18) = 'askariane_connect.';
DELETE FROM askariane_connect_enrolment;
