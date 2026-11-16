"""System monitoring: automatic checks of the things the scanner depends on, alerts when one fails, and email to the people who must act.

Why it exists: on 12 Nov the Claude API key was revoked. Nothing told anyone; reports quietly lost six checks until a person noticed a red line in the usage panel. This package finds that kind of
failure on a schedule (before a customer scan hits it), records one alert per problem, emails the configured recipients, and shows everything on the System health screen.

Isolation: the package imports no scoring and no scan logic. Only the worker (one call in its maintenance loop) and the API router import it.
"""
