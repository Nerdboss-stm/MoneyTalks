import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/inter-tight/400.css";
import "@fontsource/inter-tight/500.css";
import { Application, Graphics, Text, TextStyle } from "pixi.js";
import { COLOR, FONT, RULE_PX } from "./tokens";
import { connect } from "./ws";

async function boot(): Promise<void> {
  const canvas = document.getElementById("floor") as HTMLCanvasElement;
  const app = new Application();
  await app.init({
    canvas,
    resizeTo: window,
    background: COLOR.bg,
    antialias: false,
    resolution: window.devicePixelRatio || 1,
    autoDensity: true,
  });

  const rule = new Graphics();
  const title = new Text({
    text: "MANDATE",
    style: new TextStyle({
      fontFamily: FONT.sans,
      fontSize: 12,
      fill: COLOR.muted,
      fontWeight: "500",
    }),
  });
  title.position.set(24, 28);
  title.anchor.set(0, 0.5);

  function layout(): void {
    rule.clear();
    rule.rect(0, 48, app.screen.width, RULE_PX).fill(COLOR.rule);
  }

  app.stage.addChild(rule, title);
  layout();
  app.renderer.on("resize", layout);

  connect();
}

void boot();
