from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hello-mcp")


@mcp.tool()
def say_hello(name: str) -> str:
    """Says hello to someone."""
    return f"Hello, {name}!"


if __name__ == "__main__":
    mcp.run()

