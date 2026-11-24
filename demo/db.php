<?php
declare(strict_types=1);

function sqlite(string $file): PDO
{
    $db = new PDO('sqlite:' . $file);
    $db->exec('PRAGMA foreign_keys = ON');
    return $db;
}

function postgres(): PDO
{
    $host = getenv('PGHOST') ?: 'postgres';
    $database = getenv('PGDATABASE') ?: 'shop';
    return new PDO('pgsql:host=' . $host . ';dbname=' . $database, 'demo');
}

function schemaName(string $name): string
{
    if (!preg_match('/\Abf_[0-9a-f]{32}_[a-z0-9]{3,16}\z/', $name)) {
        throw new InvalidArgumentException('Invalid rehearsal schema');
    }
    return $name;
}

function atSchema(PDO $db, string $name): void
{
    $db->exec('SET search_path TO ' . schemaName($name));
}

function lookup(PDO $db, string $sql, string $id): array|false
{
    $statement = $db->prepare($sql);
    $statement->execute([$id]);
    return $statement->fetch(PDO::FETCH_ASSOC);
}
