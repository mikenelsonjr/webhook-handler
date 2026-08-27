import hmac
import hashlib


def compute_signature(secret: str, body: bytes) -> str:
    """
    Computes the HMAC SHA256 signature for the given payload using the provided secret.

    Args:
        secret (str): The secret key used for HMAC.
        body (bytes): The body to sign.

    Returns:
        str: The computed signature in hexadecimal format.
    """
    
    # Create a new HMAC object using the secret and SHA256
    hmac_obj = hmac.new(secret.encode(), body, hashlib.sha256)
    
    # Return the hexadecimal representation of the signature
    return hmac_obj.hexdigest()

def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """
    Verifies that the provided signature matches the computed signature for the given payload.

    Args:
        secret (str): The secret key used for HMAC.
        body (bytes): The body to verify.
        signature (str): The signature to compare against.

    Returns:
        bool: True if the signatures match, False otherwise.
    """
    
    # Compute the expected signature
    expected_signature = compute_signature(secret, body)
    
    # Use hmac.compare_digest for a secure comparison
    return hmac.compare_digest(expected_signature, signature)