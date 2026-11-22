<?php
declare(strict_types=1);

$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
header('Content-Type: application/json');
if ($path === '/ready' || $path === '/aws/lambda-microvms/runtime/v1/ready') {
    echo json_encode(['status' => 'ready'], JSON_THROW_ON_ERROR);
    exit;
}
if ($_SERVER['REQUEST_METHOD'] === 'GET' && $path === '/quote') {
    $quantity = $_GET['quantity'] ?? '';
    if (is_string($quantity) && preg_match('/\A[1-9][0-9]?\z/', $quantity)) {
        echo json_encode([
            'sku' => 'cloud-mug', 'quantity' => (int) $quantity, 'total_cents' => 2500 * (int) $quantity,
        ], JSON_THROW_ON_ERROR);
        exit;
    }
    http_response_code(400);
    echo '{"error":"Invalid quantity"}';
    exit;
}
http_response_code(404);
echo '{"error":"Not found"}';
