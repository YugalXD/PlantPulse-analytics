#!/usr/bin/env bash
# Build a throw-away database with the Laravel schema and demo data for the sidecar integration test.
# Usage: scripts/prepare_test_db.sh [db_name]   (default chp_analytics_test)
set -euo pipefail
DB="${1:-chp_analytics_test}"
cd "$(dirname "$0")/../../web"
php -r '
$pdo = new PDO("pgsql:host=".(getenv("DB_HOST") ?: "127.0.0.1").";dbname=postgres", getenv("DB_USERNAME") ?: "devuser", getenv("DB_PASSWORD") ?: "devpass");
$db = $argv[1];
if (! preg_match("/^[a-z0-9_]+$/", $db)) { fwrite(STDERR, "bad db name\n"); exit(1); }
if (! $pdo->query("select 1 from pg_database where datname = ".$pdo->quote($db))->fetchColumn()) { $pdo->exec("create database \"$db\""); }
' "$DB"
DB_DATABASE="$DB" APP_ENV=local php artisan migrate:fresh --seed --force >/dev/null
echo "Ready: postgresql+psycopg://devuser:devpass@127.0.0.1:5432/$DB"
