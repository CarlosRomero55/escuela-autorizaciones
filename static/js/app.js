// Comportamiento mínimo del portal. Sin scripts en línea: la CSP solo permite archivos propios.
(function () {
  "use strict";

  // Avisos (toasts): los de error se quedan hasta que la persona los cierra
  document.querySelectorAll(".toast").forEach(function (el) {
    var aviso = bootstrap.Toast.getOrCreateInstance(el, { autohide: el.dataset.autohide !== "false", delay: 7000 });
    aviso.show();
  });

  // Selectores que envían su formulario al cambiar (por ejemplo, el cambio de rol)
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.requestSubmit(); });
  });

  // Botones para copiar enlaces de un solo uso
  document.querySelectorAll("[data-copiar]").forEach(function (boton) {
    boton.addEventListener("click", function () {
      var objetivo = document.querySelector(boton.dataset.copiar);
      var texto = objetivo.value || objetivo.textContent;
      var original = boton.innerHTML;
      var listo = function () {
        boton.textContent = "Copiado";
        setTimeout(function () { boton.innerHTML = original; }, 2000);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(texto).then(listo, function () { objetivo.select(); });
      } else {
        objetivo.select();
      }
    });
  });

  // Búsqueda: Escape cierra los resultados
  var resultados = document.getElementById("resultados-busqueda");
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && resultados) { resultados.innerHTML = ""; }
  });
  document.addEventListener("click", function (e) {
    if (resultados && !e.target.closest(".buscador")) { resultados.innerHTML = ""; }
  });
})();
