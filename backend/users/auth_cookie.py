from rest_framework_simplejwt.authentication import JWTAuthentication

class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        header = self.get_header(request)
        if header is not None:
            return super().authenticate(request)

        raw = request.COOKIES.get("guvfx_access")
        if not raw:
            return None

        validated = self.get_validated_token(raw)
        return (self.get_user(validated), validated)
