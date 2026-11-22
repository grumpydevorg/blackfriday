<?php
declare(strict_types=1);
require __DIR__ . '/db.php';

[$script, $snapshot, $schema, $mode] = $argv;
if (!is_file($snapshot) || !in_array($mode, ['preserve', 'omit'], true)) {
    throw new InvalidArgumentException('Expected an existing SQLite checkpoint and preserve or omit');
}
$source = sqlite($snapshot);
$source->exec('PRAGMA query_only = ON');
$destination = postgres();
$destination->beginTransaction();
$destination->exec('CREATE SCHEMA ' . schemaName($schema));
atSchema($destination, $schema);
$destination->exec(file_get_contents(__DIR__ . '/schema.sql'));

// Both releases copy identities and order history. Only one copies carts.
$tables = ['customers' => '?, ?', 'orders' => '?, ?, ?, ?, ?, ?'];
if ($mode === 'preserve') {
    $tables['carts'] = '?, ?, ?';
}
foreach ($tables as $table => $parameters) {
    $insert = $destination->prepare("INSERT INTO $table VALUES ($parameters)");
    foreach ($source->query("SELECT * FROM $table", PDO::FETCH_NUM) as $row) {
        $insert->execute($row);
    }
}
$destination->commit();
