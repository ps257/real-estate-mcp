"""Real Estate Market Intelligence — FastMCP server package."""

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

__version__ = "0.1.0"
