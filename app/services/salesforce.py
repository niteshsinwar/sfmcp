"""Salesforce connection management with OAuth support"""
from simple_salesforce import Salesforce
import threading
import time
import logging

logger = logging.getLogger(__name__)

# Import OAuth functions
try:
    from app.mcp.tools.oauth_auth import get_stored_tokens, refresh_salesforce_token
    OAUTH_AVAILABLE = True
except ImportError:
    OAUTH_AVAILABLE = False
    logger.warning("⚠️ OAuth module not available")

# Thread-local storage
local = threading.local()

def get_salesforce_connection(user_id: str = None):
    """
    Get Salesforce connection using OAuth tokens (no config required).
    
    Args:
        user_id: Specific user ID (optional, uses first available)
        
    Returns:
        Salesforce connection instance
    """
    if not hasattr(local, 'sf_connection') or local.sf_connection is None:
        logger.info("🔗 Creating Salesforce connection...")
        
        if not OAUTH_AVAILABLE:
            raise Exception("OAuth not available. Please ensure oauth_auth module is imported.")
        
        stored_tokens = get_stored_tokens()
        if not stored_tokens:
            raise Exception(
                "❌ No active Salesforce sessions found.\n"
                "Please run one of these commands first:\n"
                "- salesforce_production_login() - for production orgs\n"
                "- salesforce_sandbox_login() - for sandbox orgs\n"
                "- salesforce_custom_login('https://yourorg.my.salesforce.com') - for custom domains"
            )
        
        # Select token
        if user_id and user_id in stored_tokens:
            token_data = stored_tokens[user_id]
            selected_user = user_id
        else:
            selected_user, token_data = next(iter(stored_tokens.items()))
        
        # Check token age and refresh if needed (90 minutes)
        token_age = time.time() - token_data['login_timestamp']
        if token_age > 5400:
            logger.info(f"🔄 Refreshing token for {selected_user}...")
            if not refresh_salesforce_token(selected_user):
                raise Exception(f"Failed to refresh token for {selected_user}. Please login again.")
            # Get updated token
            token_data = get_stored_tokens()[selected_user]
        
        # Create connection
        local.sf_connection = Salesforce(
            instance_url=token_data['instance_url'],
            session_id=token_data['access_token']
        )
        
        logger.info(f"✅ Connected to {token_data['instance_url']} as user {selected_user}")
    
    return local.sf_connection

def clear_connection_cache():
    """Clear connection cache to force new connection"""
    if hasattr(local, 'sf_connection'):
        local.sf_connection = None
