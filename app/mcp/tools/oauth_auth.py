import webbrowser
import threading
import time
import json
import requests
import secrets
import psutil  # Added for port management
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from typing import Dict, Any

from app.mcp.server import register_tool

# Global storage
_oauth_tokens = {}
_oauth_callback_data = {}

class SalesforceCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)
        
        if parsed_url.path in ['/', '/OauthRedirect']:
            if 'code' in query_params:
                auth_code = query_params['code'][0]
                state = query_params.get('state', [None])[0]
                
                _oauth_callback_data[state] = {
                    'code': auth_code,
                    'timestamp': time.time()
                }
                
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body><h2>Login Success! Close this window.</h2></body></html>')
                
            elif 'error' in query_params:
                error = query_params['error'][0]
                _oauth_callback_data['error'] = {'error': error}
                self.send_response(400)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body><h2>Login Failed</h2></body></html>')
    
    def log_message(self, format, *args):
        pass

def _free_port(port: int) -> None:
    """Free up the specified port by terminating any processes using it"""
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    proc.terminate()
                    proc.wait(timeout=3)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                    pass  # Ignore errors - process may have already closed
        # Small delay to ensure port is fully released
        time.sleep(0.5)
    except Exception:
        pass  # Ignore any errors in port cleanup

def _start_callback_server(port: int = 1717):
    """Start callback server after ensuring port is free"""
    # Automatically free the port before starting server
    _free_port(port)
    
    server = HTTPServer(('localhost', port), SalesforceCallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server

def _create_json_response(success, **kwargs):
    """Create guaranteed valid JSON response"""
    result = {"success": success}
    
    # Only add safe, JSON-serializable values
    for key, value in kwargs.items():
        if value is None:
            result[key] = None
        elif isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, (list, dict)):
            result[key] = value
        else:
            result[key] = str(value)
    
    # Return clean JSON string
    return json.dumps(result, indent=2)

@register_tool
def salesforce_production_login() -> str:
    """Login to Salesforce production org."""
    return _do_login("production", "https://login.salesforce.com")

@register_tool
def salesforce_sandbox_login() -> str:
    """Login to Salesforce sandbox org."""
    return _do_login("sandbox", "https://test.salesforce.com")

@register_tool
def salesforce_custom_login(domain_url: str) -> str:
    """Login to custom Salesforce domain."""
    clean_domain = domain_url.rstrip('/')
    return _do_login("custom", clean_domain)

@register_tool
def free_oauth_port(port: int = 1717) -> str:
    """
    Manually free the OAuth callback port if needed.
    Default port is 1717 used for Salesforce OAuth callback.
    """
    try:
        _free_port(port)
        return _create_json_response(
            True, 
            message=f"Port {port} freed successfully"
        )
    except Exception as e:
        return _create_json_response(
            False, 
            error=f"Failed to free port {port}: {str(e)}"
        )

def _do_login(org_type: str, auth_url: str) -> str:
    """Perform OAuth login with automatic port cleanup and guaranteed JSON response"""
    try:
        # Setup
        state = secrets.token_urlsafe(16)  # Shorter, safer token
        callback_port = 1717
        redirect_uri = f"http://localhost:{callback_port}/OauthRedirect"
        
        # Start server (this will automatically free the port first)
        server = _start_callback_server(callback_port)
        
        # Build URL
        params = {
            'response_type': 'code',
            'client_id': 'PlatformCLI',
            'redirect_uri': redirect_uri,
            'state': state,
            'scope': 'api web refresh_token'
        }
        
        login_url = auth_url + "/services/oauth2/authorize?" + urlencode(params)
        
        # Open browser
        webbrowser.open(login_url)
        
        # Wait for callback (simplified)
        timeout = 300
        start_time = time.time()
        
        while (time.time() - start_time) < timeout:
            if state in _oauth_callback_data:
                # Success case
                callback_data = _oauth_callback_data.pop(state)
                try:
                    server.shutdown()
                except:
                    pass  # Ignore server shutdown errors
                
                # Get token
                token_url = f"{auth_url}/services/oauth2/token"
                token_data = {
                    'grant_type': 'authorization_code',
                    'client_id': 'PlatformCLI',
                    'redirect_uri': redirect_uri,
                    'code': callback_data['code']
                }
                
                response = requests.post(token_url, data=token_data, timeout=30)
                response.raise_for_status()
                token_response = response.json()
                
                # Store token
                user_id = token_response.get('id', '').split('/')[-1] or 'user'
                _oauth_tokens[user_id] = {
                    'access_token': token_response['access_token'],
                    'refresh_token': token_response.get('refresh_token'),
                    'instance_url': token_response['instance_url'],
                    'user_id': user_id,
                    'login_timestamp': time.time(),
                    'org_type': org_type
                }
                
                return _create_json_response(
                    True,
                    message="Login successful",
                    user_id=user_id,
                    instance_url=token_response['instance_url'],
                    org_type=org_type
                )
            
            elif 'error' in _oauth_callback_data:
                # Error case
                error_data = _oauth_callback_data.pop('error')
                try:
                    server.shutdown()
                except:
                    pass
                return _create_json_response(
                    False,
                    error="Login failed",
                    details=error_data.get('error', 'Unknown error')
                )
            
            time.sleep(1)
        
        # Timeout case
        try:
            server.shutdown()
        except:
            pass
        return _create_json_response(False, error="Login timeout")
        
    except Exception as e:
        return _create_json_response(False, error=f"Login error: {str(e)}")

@register_tool
def salesforce_logout() -> str:
    """Clear stored tokens."""
    try:
        count = len(_oauth_tokens)
        _oauth_tokens.clear()
        _oauth_callback_data.clear()
        return _create_json_response(True, message=f"Logged out {count} sessions")
    except Exception as e:
        return _create_json_response(False, error=str(e))

@register_tool
def salesforce_auth_status() -> str:
    """Check authentication status."""
    try:
        if not _oauth_tokens:
            return _create_json_response(
                True, 
                authenticated=False, 
                message="No active sessions"
            )
        
        sessions = []
        for user_id, token_info in _oauth_tokens.items():
            age_minutes = round((time.time() - token_info['login_timestamp']) / 60, 1)
            sessions.append({
                "user_id": user_id,
                "instance_url": token_info['instance_url'],
                "org_type": token_info.get('org_type', 'unknown'),
                "age_minutes": age_minutes
            })
        
        return _create_json_response(
            True,
            authenticated=True,
            sessions=sessions,
            total_sessions=len(sessions)
        )
    except Exception as e:
        return _create_json_response(False, error=str(e))

# Export functions
def get_stored_tokens():
    return _oauth_tokens.copy()

def refresh_salesforce_token(user_id: str) -> bool:
    if user_id not in _oauth_tokens:
        return False
    
    token_info = _oauth_tokens[user_id]
    refresh_token = token_info.get('refresh_token')
    if not refresh_token:
        return False
    
    try:
        refresh_url = f"{token_info['instance_url']}/services/oauth2/token"
        data = {
            'grant_type': 'refresh_token',
            'client_id': 'PlatformCLI',
            'refresh_token': refresh_token
        }
        
        response = requests.post(refresh_url, data=data, timeout=30)
        response.raise_for_status()
        new_token = response.json()
        
        token_info.update({
            'access_token': new_token['access_token'],
            'login_timestamp': time.time()
        })
        
        return True
    except:
        return False
