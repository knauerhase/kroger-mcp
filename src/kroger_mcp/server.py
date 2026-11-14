#!/usr/bin/env python3
"""
FastMCP Server for Kroger API

This server provides MCP tools for interacting with the Kroger API, including:
- Location management (search stores, get details, set preferred location)
- Product search and details
- Cart management (add items, bulk operations, tracking)
- Chain and department information
- User profile and authentication

Environment Variables Required:
- KROGER_CLIENT_ID: Your Kroger API client ID
- KROGER_CLIENT_SECRET: Your Kroger API client secret
- KROGER_REDIRECT_URI: Redirect URI for OAuth2 flow (default: http://localhost:8000/callback)
- KROGER_USER_ZIP_CODE: Default zip code for location searches (optional)
"""

from fastmcp import FastMCP

from kroger_api.utils.env import load_env_file

# Import all tool modules
from .tools import location_tools
from .tools import product_tools
from .tools import cart_tools
from .tools import draft_tools
from .tools import info_tools
from .tools import profile_tools
from .tools import utility_tools
from .tools import auth_tools

# Import prompts
from . import prompts
from .errors import ToolErrorMiddleware


def create_server() -> FastMCP:
    """Create and configure the FastMCP server instance"""
    # A .env file in the working directory supplements the environment
    load_env_file()

    # Initialize the FastMCP server
    mcp = FastMCP(
        name="Kroger API Server",
        instructions="""
        This MCP server provides access to Kroger's API for grocery shopping functionality.
        
        Key Features:
        - Search and manage store locations
        - Find and search products
        - Collect items in a draft cart the user reviews, then send them to the Kroger cart
        - Access chain and department information
        - User profile management
        
        The Kroger cart is add-only: the Kroger API cannot remove items, lower quantities, or read
        the cart, and adding a product already in the cart raises its quantity. Only the user can
        undo an add, in the Kroger app or website. So collect items in the draft first.
        
        Common workflows:
        1. Set a preferred location with set_preferred_location
        2. Search for products with search_products or bulk_search_products
        3. Put the chosen products in the draft with add_to_draft
        4. Show the user the draft with view_draft, and change it with update_draft_item or remove_from_draft
        5. Once the user approves, send it with send_draft_to_cart
        6. View what was sent with view_current_cart
        7. Mark order as placed with mark_order_placed
        Use add_items_to_cart or bulk_add_to_cart only when the user asks to add items right away.
        
        Authentication Flow:
        1. Use start_authentication to get an authorization URL
        2. Open the URL in your browser and authorize the application
        3. Copy the full redirect URL from your browser
        4. Use complete_authentication with the redirect URL to finish the process
        
        Cart Tracking:
        This server keeps a local record of the items it sent to the cart, since the Kroger API
        doesn't provide cart viewing functionality. When you place an order through the Kroger
        website/app, use mark_order_placed to move the current cart to order history.
        
        Errors:
        A tool that fails returns an error result. Its text says what went wrong and the next
        step to take. Its structured content holds an "error" object with a "type" (such as
        invalid_arguments, invalid_parameter, authentication_required, not_found, or
        rate_limited), a "next_step", and for Kroger API errors the HTTP status, Kroger's error
        code and message, and a correlation id. Follow the next step instead of retrying
        the same call unchanged.
        """
    )

    # Return tool failures as error results with structured details
    mcp.add_middleware(ToolErrorMiddleware())

    # Register all tools from the modules
    location_tools.register_tools(mcp)
    product_tools.register_tools(mcp)
    cart_tools.register_tools(mcp)
    draft_tools.register_tools(mcp)
    info_tools.register_tools(mcp)
    profile_tools.register_tools(mcp)
    utility_tools.register_tools(mcp)
    auth_tools.register_tools(mcp)
    
    # Register prompts
    prompts.register_prompts(mcp)
    
    return mcp


def main():
    """Run the server; the command-line options are in kroger_mcp.cli"""
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
