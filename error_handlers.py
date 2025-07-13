import logging
from flask import render_template

def register_error_handlers(app):
    @app.errorhandler(400)
    def bad_request(error):
        return render_template("errors/client/bad_request_400.html"), 400

    @app.errorhandler(401)
    def unauthorized(error):
        return render_template("errors/client/unauthorized_401.html"), 401

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("errors/client/forbidden_403.html"), 403

    @app.errorhandler(404)
    def page_not_found(error):
        return render_template("errors/client/not_found_404.html"), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        return render_template("errors/client/method_not_allowed_405.html"), 405

    @app.errorhandler(412)
    def precondition_failed(error):
        return render_template("errors/client/precondition_failed_412.html"), 412

    @app.errorhandler(413)
    def request_entity_too_large(error):
        return render_template("errors/client/request_entity_too_large_413.html"), 413

    @app.errorhandler(415)
    def unsupported_media_type(error):
        return render_template("errors/client/unsupported_media_type_415.html"), 415

    @app.errorhandler(418)
    def im_a_teapot(error):
        return render_template("errors/client/im_a_teapot_418.html"), 418

    @app.errorhandler(429)
    def too_many_requests(error):
        logging.warning(f"Rate limit exceeded: {error}")
        return render_template("errors/client/too_many_requests_429.html"), 429

    @app.errorhandler(451)
    def unavailable_for_legal_reasons(error):
        return render_template("errors/client/unavailable_for_legal_reasons_451.html"), 451

    @app.errorhandler(500)
    def internal_server_error(error):
        return render_template("errors/server/internal_server_error_500.html"), 500

    @app.errorhandler(501)
    def not_implemented(error):
        return render_template("errors/server/not_implemented_501.html"), 501

    @app.errorhandler(502)
    def bad_gateway(error):
        return render_template("errors/server/bad_gateway_502.html"), 502

    @app.errorhandler(503)
    def service_unavailable(error):
        return render_template("errors/server/service_unavailable_503.html"), 503