# Cua Driver SDK em vez do endpoint MCP

Decisão do dono, 2026-09-30: o adaptador do `laya-computer` passa a usar o SDK Python tipado `cua-driver` (`cua_driver.CuaDriver.call_tool`), fixado em `0.31.0`, em vez de um cliente MCP por stdio para `cua-driver mcp`.

Motivos: sem subprocesso nem serialização MCP entre o harness e o driver; o resultado de cada ação chega tipado (`ToolResult.action` com `effect`, `route`, `delivery`, `escalation`, `error`) e erros de ferramenta não viram exceções de transporte; o modo daemon (`CuaDriver.connect`) continua disponível por `CUA_DRIVER_SOCKET`.

Custos aceitos: o SDK é pré-1.0 e a versão embarcada pode diferir do `cua-driver` instalado (0.23.2 na investigação), por isso o pin exato; no modo embarcado as permissões do macOS pertencem ao processo hospedeiro, não ao `CuaDriver.app`. O servidor continua falando MCP com o modelo chamador; só o lado do driver mudou.
