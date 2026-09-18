-- Temporal's auto-setup creates its schema, but not the databases themselves.
-- These run once, on first initialisation of an empty data volume.
--
-- Temporal is durable execution only. No scientific record lives in these
-- databases; the authoritative Project State is the `ravel` database.

CREATE DATABASE temporal OWNER ravel;
CREATE DATABASE temporal_visibility OWNER ravel;
