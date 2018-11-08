from shepherd.schema import FlockIdSchema, FlockRequestOptsSchema, GenericResponseSchema
from shepherd.schema import LaunchResponseSchema

from flask import Response


# ============================================================================
def init_routes(app):
    @app.route(['/api/request_flock/<flock>', '/api/<pool>/request_flock/<flock>'], methods=['POST'], endpoint='request_flock',
               req_schema=FlockRequestOptsSchema,
               resp_schema=GenericResponseSchema)
    def request_flock(flock, pool='', **kwargs):
        """Request a new flock
        ---
        post:
            summary: Request a new flock by flock id
            parameters:
                - in: path
                  name: flock
                  schema: FlockIdSchema
                  description: Flock id from the flocks.yaml file

            requestBody:
                description: optional user params, environment, and image overrides
                required: false
                content:
                    application/json:
                        schema: FlockRequestOptsSchema

            responses:
                200:
                    description: A flock response
                    schema: GenericResponseSchema

                400:
                    schema: GenericResponseSchema

                404:
                    schema: GenericResponseSchema
        """
        return app.get_pool(pool).request(flock, kwargs.get('request'))


    @app.route(['/api/start_flock/<reqid>', '/api/<pool>/start_flock/<reqid>'], methods=['POST'],
               resp_schema=LaunchResponseSchema)
    def start_flock(reqid, pool=''):
        """Start a flock from reqid
        ---
        post:
            summary: Request a new flock by flock id
            parameters:
                - in: path
                  name: reqid
                  schema: {type: string}
                  description: a unique request id that was created from flock request

            responses:
                200:
                    description: A flock launch response
                    schema: LaunchResponseSchema

                400:
                    schema: GenericResponseSchema

                404:
                    schema: GenericResponseSchema
        """
        return app.get_pool(pool).start(reqid)

    @app.route(['/api/stop_flock/<reqid>', '/api/<pool>/stop_flock/<reqid>'], methods=['POST'],
               resp_schema=GenericResponseSchema)
    def stop_flock(reqid, pool=''):
        """Stop a flock from reqid
        ---
        post:
            summary: Stop flock by id
            parameters:
                - in: path
                  name: reqid
                  schema: {type: string}
                  description: a unique request id that was created from flock request

            responses:
                200:
                    description: Returns 'success' if stopped
                    schema: GenericResponseSchema

                400:
                    schema: GenericResponseSchema

                404:
                    schema: GenericResponseSchema
        """
        app.get_pool(pool).stop(reqid)
        return {'success': True}


    @app.route('/api', methods=['GET'])
    def print_api():
        return Response(app.apispec.to_yaml(), mimetype='text/yaml')

