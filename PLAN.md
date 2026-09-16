# Plan : Knowledge Graph FastAPI

Objectif : construire un graphe **typé et spécifique à FastAPI** (routes, handlers, dépendances,
schémas, modèles ORM, tables SQL, templates), déterministe et sans LLM. Tous les outils
MCP pourront l'interroger. Ce graphe complète graphify (qui reste générique) sans le dupliquer.

## Projets de validation

| Projet | Style | Ce qu'il teste |
|---|---|---|
| `FileRouge/backend` | Structure classique : `APIRouter` ×7, `Depends(get_db / get_current_user / require_admin)`, 22 modèles ORM `Base`, 37 `BaseModel` | Routers, préfixes, arbres de deps, ORM ↔ schémas |
| `TechFi24` | Un seul `main.py` avec `@app.get`, **pas de Depends ni d'ORM** : SQL brut psycopg2, auth manuelle (`_is_admin_authenticated(request)`, `Header(None)`), templates Jinja2 | Graphe d'appels, tables SQL extraites des requêtes, templates, auth hors `Depends` |

**Constat :** sur TechFi24, un graphe limité à `Depends`/ORM serait presque vide. Il faut donc
aussi modéliser les **appels de fonctions** et les **tables SQL touchées**.

## Modèle du graphe

Nœuds : `Module`, `App`, `Router`, `Route`, `Handler`, `Function`, `Dependency`, `Schema`,
`ORMModel`, `Table`, `Template`, `Middleware`

Arêtes :

```
App/Router ──INCLUDES──▶ Router        (prefix → chemin complet)
Route ──HANDLED_BY──▶ Handler
Handler/Dependency ──DEPENDS_ON──▶ Dependency
Handler ──ACCEPTS──▶ Schema            Handler ──RETURNS──▶ Schema
Handler/Function ──CALLS──▶ Function   (appels résolus dans le projet uniquement)
Function ──QUERIES──▶ Table            (SQL brut : FROM / INTO / UPDATE / JOIN)
ORMModel ──MAPS_TO──▶ Table            ORMModel ──RELATES_TO──▶ ORMModel
Schema ──INHERITS──▶ Schema            Schema ──MIRRORS──▶ ORMModel (heuristique + confiance)
Handler ──RENDERS──▶ Template
```

ID qualifiés : `app.models.database:ForumEvent`.

## Phases

### Phase 0 : Fondations
- [x] Helper `iter_python_files()` qui exclut `venv`, `.venv`, `.git`, `node_modules`, `__pycache__`, etc.
- [x] Projet fixture `tests/fixtures/sample_app/` (routers imbriqués, Annotated deps, ORM, héritage)
- [x] pytest et tests de non-régression sur les 12 outils existants
- [x] Bug corrigé : `go_to_definition` s'arrêtait sur l'import (`follow_imports=True`)
- Bugs connus (tests `xfail`) : préfixes de routers ignorés, `dependencies=[...]` ignoré par `build_dependency_graph`

### Phase 1 : Extraction
- [x] `fastapi_architect/graph/` : `model.py`, `extract.py`, `resolve.py`, `builder.py`
- [x] Table d'imports par module (imports relatifs inclus) et résolution des noms qualifiés (+ correspondance par suffixe)
- [x] Routers et préfixes (`APIRouter(prefix=)` et `include_router(prefix=)`) → `full_paths` des routes
- [x] Routes (`api_route`, `websocket` inclus), deps (defaults, `kw_defaults`, `Annotated` inline/alias, décorateur, router, include), schémas, ORM, `relationship`/`ForeignKey`
- [x] Graphe d'appels (CALLS/USES), tables SQL (QUERIES, avec filtrage des docstrings, commentaires et littéraux), templates (RENDERS), middlewares
- [x] Heuristique MIRRORS (suffixes de noms + ratio de champs communs)
- [x] Stockage `networkx` + sérialisation JSON
- [x] Parcours de fichiers élagué (`os.walk`) : FileRouge 5,8 s → 1,6 s
- Validation : FileRouge → 29 routes, 13 ORM, 40 schémas, 31 tables ; TechFi24 → 26 routes, 6 tables, 9 templates

### Phase 2 : Cache et incrémental
- [x] `.fastapi-architect/graph.json` : faits d'extraction par fichier, signature (mtime, taille) + sha1, `.gitignore` auto
- [x] Reparse uniquement des fichiers modifiés, graphe mémorisé en mémoire (FileRouge : 1,5 s à froid → 0,14 s disque → 0,03 s mémoire)
- [x] `list_routes`, `get_dependencies`, `build_dependency_graph` réécrits sur le graphe
  - chemins complets avec préfixes, `api_route(methods=[...])` pris en charge
  - dépendances router / include_router / décorateur, dans l'ordre d'exécution FastAPI
  - `input_models` limité aux vrais schémas (plus de `Request`/`Session` injectés)
  - noms ambigus signalés avec la liste des candidats
- [x] Bugs connus corrigés (les 2 tests xfail passent)

### Phase 3 : Outils MCP
- [x] `build_knowledge_graph(project_root, force=False)` : stats, fichiers reparsés, erreurs de parsing
- [x] `graph_neighbors(node, depth, edge_types, direction)` : nœud par id, route (`GET /path`), table ou nom
- [x] `impact_analysis(symbol)` : parcours inverse groupé par type, chaîne `via` pour chaque route
- [x] `find_path(source, target)` : plus courts chemins, orientés puis non orientés
- [x] `audit_graph(auth_dependencies)` : écritures sans auth (Depends, Header, appels manuels), doublons, routers non montés, schémas inutilisés, ORM non référencés, cycles
- [x] `graph_report` (outil Markdown plutôt qu'une resource MCP, qui exigerait la racine du projet dans l'URI)
- [x] Extraction enrichie : types des champs (schémas imbriqués), références au niveau module, tables système SQL ignorées
- Validation : détection d'auth correcte sur toutes les routes de FileRouge et TechFi24 ; 9 schémas réellement inutilisés dans FileRouge (anciens schémas d'auth)

### Phase 4 : Visualisation
- [x] `export_graph_html()` : HTML autonome (vis-network via CDN), couleurs et formes par type, recherche, filtres par type de nœud et d'arête, panneau de détail avec voisins cliquables, lien direct `#node=<id>`
- [x] Vue « FastAPI » par défaut : fonctions et classes affichées seulement si elles touchent un nœud FastAPI ; modules et fichiers de test masqués
- [x] `graph_report(save=True)` → `.fastapi-architect/GRAPH_REPORT.md`
- Validation : rendu vérifié dans Chrome headless sur FileRouge (204/1301 nœuds affichés par défaut) et TechFi24 (118/487)

### Phase 5 : Finitions
- [ ] Validation sur FileRouge et TechFi24
- [ ] README, version 0.3.0

## Limites connues (analyse statique)
Routes dynamiques (`add_api_route` avec variables), deps passées par variable, appels via
injection/repository, SQL construit dynamiquement : couverture partielle.
