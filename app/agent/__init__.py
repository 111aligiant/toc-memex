"""Built-in research agent and run/step models for the console.

Keep this package initializer light: ``app.db.init_db`` imports
``app.agent.models`` on every entry point, and pulling in the LLM/tool stack
there would make CLI startup depend on it.
"""
