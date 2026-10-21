"""Default saved nodes and public connection candidates, grouped by network."""

# Warthog Core's default private RPC ports are 3000 (mainnet) and 3100 (--testnet).
BUNDLED_NODES = (
    ('Local node', 'http://localhost:3000', 'mainnet'),
    ('Wartnode mainnet', 'https://mainnet.wartnode.com', 'mainnet'),
    ('Warthog mainnet', 'https://warthognode.duckdns.org', 'mainnet'),
    ('Local DeFi testnet', 'http://localhost:3100', 'testnet'),
    ('Wartnode DeFi testnet', 'https://testnet.wartnode.com', 'testnet'),
    ('Warthog DeFi testnet', 'https://warthog-defitestnet.duckdns.org', 'testnet'),
)

PUBLIC_NODES = [url for _, url, network in BUNDLED_NODES if network == 'mainnet' and url.startswith('https://')]
TESTNET_NODES = [url for _, url, network in BUNDLED_NODES if network == 'testnet' and url.startswith('https://')]
