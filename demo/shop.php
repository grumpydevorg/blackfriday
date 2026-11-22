<?php
declare(strict_types=1);
require __DIR__ . '/db.php';

function reply(int $code, array $body): never
{
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode($body, JSON_THROW_ON_ERROR);
    exit;
}

function checkout(PDO $db, string $customer, string $cart): never
{
    $intention = $_SERVER['HTTP_IDEMPOTENCY_KEY'] ?? '';
    if (!preg_match('/\A[a-z][a-z0-9-]{1,39}\z/', $intention)) {
        reply(400, ['error' => 'Idempotency-Key required']);
    }
    $prior = lookup($db, 'SELECT * FROM orders WHERE intention = ?', $intention);
    if ($prior !== false) {
        if ($prior['customer_id'] !== $customer || $prior['cart_id'] !== $cart) {
            reply(409, ['error' => 'Intention already used for another purchase']);
        }
        reply(200, $prior);
    }
    $saved = lookup($db, 'SELECT * FROM carts WHERE id = ?', $cart);
    if ($saved === false || $saved['customer_id'] !== $customer) {
        reply(404, ['error' => 'Cart not found']);
    }
    if (lookup($db, 'SELECT id FROM orders WHERE cart_id = ?', $cart) !== false) {
        reply(409, ['error' => 'Cart already ordered']);
    }

    $payment = json_encode([
        'scope' => getenv('PAYMENT_SCOPE'), 'intention' => $intention,
        'customer_id' => $customer, 'cart_id' => $cart, 'amount_cents' => 2500,
    ], JSON_THROW_ON_ERROR);
    $context = stream_context_create(['http' => [
        'method' => 'POST', 'header' => 'Content-Type: application/json',
        'content' => $payment, 'timeout' => 3, 'follow_location' => 0,
    ]]);
    // The referee can commit the payment and deliberately close without a response.
    $receipt = @file_get_contents(getenv('PAYMENT_URL'), false, $context);
    if ($receipt === false) {
        reply(502, ['error' => 'Payment response unavailable; retry with the same key']);
    }
    $paymentResult = json_decode($receipt, true, flags: JSON_THROW_ON_ERROR);
    $order = [
        'id' => 'order-' . $intention, 'customer_id' => $customer, 'cart_id' => $cart,
        'intention' => $intention, 'amount_cents' => 2500, 'receipt' => $paymentResult['receipt'],
    ];
    $db->prepare('INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)')->execute(array_values($order));
    reply(200, $order);
}

try {
    $schema = getenv('SHOP_SCHEMA');
    if ($schema !== false) {
        $db = postgres();
        atSchema($db, $schema);
    } else {
        $db = sqlite(getenv('SHOP_SQLITE'));
    }
    $path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
    $method = $_SERVER['REQUEST_METHOD'];
    if ($method === 'GET' && $path === '/health') {
        // Readiness checks DB access, not migration completeness.
        $db->query('SELECT COUNT(*) FROM customers')->fetchColumn();
        reply(200, ['status' => 'ready', 'database' => $db->getAttribute(PDO::ATTR_DRIVER_NAME)]);
    }
    if ($method === 'GET' && $path === '/product') {
        reply(200, ['sku' => 'cloud-mug', 'name' => 'Cloud Mug', 'price_cents' => 2500]);
    }
    $customer = $_SERVER['HTTP_X_CUSTOMER_ID'] ?? '';
    if (lookup($db, 'SELECT id FROM customers WHERE id = ?', $customer) === false) {
        reply(404, ['error' => 'Customer not found']);
    }
    if ($method === 'GET' && preg_match('~\A/(cart|orders)/([a-z0-9-]+)\z~', $path, $match)) {
        $sql = $match[1] === 'cart' ? 'SELECT * FROM carts WHERE id = ?' : 'SELECT * FROM orders WHERE id = ?';
        $row = lookup($db, $sql, $match[2]);
        if ($row === false || $row['customer_id'] !== $customer) {
            reply(404, ['error' => $match[1] === 'cart' ? 'Cart not found' : 'Order not found']);
        }
        reply(200, $row);
    }
    if ($method === 'POST' && ($path === '/cart' || $path === '/checkout')) {
        $body = json_decode(file_get_contents('php://input'), true, flags: JSON_THROW_ON_ERROR);
        $cart = $body['cart_id'] ?? '';
        if (!is_string($cart) || !preg_match('/\A[a-z][a-z0-9-]{1,39}\z/', $cart)) {
            reply(400, ['error' => 'A valid cart_id is required']);
        }
        if ($path === '/checkout') {
            checkout($db, $customer, $cart);
        }
        if (($body['sku'] ?? null) !== 'cloud-mug') {
            reply(400, ['error' => 'Only the Cloud Mug is sold here']);
        }
        $db->prepare('INSERT INTO carts VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING')
            ->execute([$cart, $customer, 'cloud-mug']);
        $saved = lookup($db, 'SELECT * FROM carts WHERE id = ?', $cart);
        if ($saved === false || $saved['customer_id'] !== $customer) {
            reply(409, ['error' => 'Cart ID already in use']);
        }
        reply(200, $saved);
    }
    reply(404, ['error' => 'Route not found']);
} catch (JsonException) {
    reply(400, ['error' => 'Invalid JSON']);
} catch (Throwable $error) {
    error_log((string) $error);
    reply(500, ['error' => 'Shop error']);
}
