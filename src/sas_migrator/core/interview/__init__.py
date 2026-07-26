"""Entrevistas human-in-the-loop (Etapa 3) — construcción determinista.

Este paquete es core puro: construye los payloads tipados (`InterviewCard`)
desde la evidencia en ``state/``, valida respuestas (`validate`) y escribe los
artefactos de decisión (`apply`). NO conoce LangGraph ni MCP: los nodos del
grafo solo llaman ``interrupt(card)``; el transporte es problema de otros.
"""
