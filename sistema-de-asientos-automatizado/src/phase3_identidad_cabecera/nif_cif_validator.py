"""
Validador de identificadores fiscales españoles: NIF, CIF, NIE.

Implementa checksums oficiales según BOE:
- NIF: módulo 23 con tabla de letras
- CIF: algoritmo de suma par/impar
- NIE: sustitución X/Y/Z + algoritmo NIF
"""

import re
from dataclasses import dataclass
from enum import Enum


class TipoIdentificador(Enum):
    """Tipos de identificadores fiscales españoles."""
    NIF = "nif"
    CIF = "cif"
    NIE = "nie"
    DESCONOCIDO = "desconocido"


@dataclass
class ResultadoValidacion:
    """Resultado de validación de un identificador fiscal."""
    es_valido: bool
    tipo: TipoIdentificador
    formato_ok: bool
    checksum_ok: bool
    razon: str


# Tabla de letras para NIF/NIE (módulo 23)
_TABLA_NIF = "TRWAGMYFPDXBNJZSQVHLCKE"

# Patrones regex para detección de tipo
_PATRON_NIF = re.compile(r"^(\d{8})([A-Z])$")
_PATRON_CIF = re.compile(r"^([ABCDEFGHJKLMNPQRSUVW])(\d{7})([0-9A-J])$")
_PATRON_NIE = re.compile(r"^([XYZ])(\d{7})([A-Z])$")


def validar_identificador_fiscal(valor: str) -> ResultadoValidacion:
    """
    Validar un identificador fiscal español (NIF, CIF o NIE).

    Args:
        valor: String con el identificador (ej: "A46103834", "12345678Z", "X1234567L")

    Returns:
        ResultadoValidacion con tipo detectado, validación de formato y checksum
    """
    if not valor:
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.DESCONOCIDO,
            formato_ok=False,
            checksum_ok=False,
            razon="Valor vacío"
        )

    # Limpiar y normalizar
    valor_limpio = valor.strip().upper()

    # Intentar detectar tipo y validar
    # 1. NIF
    match = _PATRON_NIF.match(valor_limpio)
    if match:
        numero = match.group(1)
        letra = match.group(2)
        return _validar_nif(numero, letra)

    # 2. CIF
    match = _PATRON_CIF.match(valor_limpio)
    if match:
        letra_org = match.group(1)
        numero = match.group(2)
        control = match.group(3)
        return _validar_cif(letra_org, numero, control)

    # 3. NIE
    match = _PATRON_NIE.match(valor_limpio)
    if match:
        letra_inicial = match.group(1)
        numero = match.group(2)
        letra_control = match.group(3)
        return _validar_nie(letra_inicial, numero, letra_control)

    # No coincide con ningún patrón
    return ResultadoValidacion(
        es_valido=False,
        tipo=TipoIdentificador.DESCONOCIDO,
        formato_ok=False,
        checksum_ok=False,
        razon=f"Formato no reconocido: '{valor_limpio}'"
    )


def _validar_nif(numero: str, letra: str) -> ResultadoValidacion:
    """
    Validar NIF con algoritmo de módulo 23.

    Args:
        numero: 8 dígitos del NIF
        letra: Letra de control

    Returns:
        ResultadoValidacion
    """
    try:
        num_int = int(numero)
        letra_esperada = _TABLA_NIF[num_int % 23]

        if letra == letra_esperada:
            return ResultadoValidacion(
                es_valido=True,
                tipo=TipoIdentificador.NIF,
                formato_ok=True,
                checksum_ok=True,
                razon="NIF válido"
            )
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.NIF,
            formato_ok=True,
            checksum_ok=False,
            razon=f"Checksum NIF incorrecto: esperada '{letra_esperada}', encontrada '{letra}'"
        )
    except ValueError:
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.NIF,
            formato_ok=False,
            checksum_ok=False,
            razon="Error al parsear número NIF"
        )


def _validar_cif(letra_org: str, numero: str, control: str) -> ResultadoValidacion:
    """
    Validar CIF con algoritmo de suma par/impar.

    Args:
        letra_org: Letra de tipo de organización (A-W)
        numero: 7 dígitos centrales
        control: Dígito/letra de control

    Returns:
        ResultadoValidacion
    """
    try:
        # Algoritmo de suma par/impar
        suma = 0

        # Dígitos en posición impar (1º, 3º, 5º, 7º): multiplicar por 2
        for i in [0, 2, 4, 6]:
            digito = int(numero[i])
            doble = digito * 2
            # Si el resultado es > 9, sumar sus dígitos
            if doble > 9:
                suma += (doble // 10) + (doble % 10)
            else:
                suma += doble

        # Dígitos en posición par (2º, 4º, 6º): sumar directamente
        for i in [1, 3, 5]:
            suma += int(numero[i])

        # Obtener dígito de control
        unidad = suma % 10
        digito_control = (10 - unidad) % 10

        # Algunos CIF usan letra en lugar de dígito
        # Tabla de conversión: 0→J, 1→A, 2→B, 3→C, 4→D, 5→E, 6→F, 7→G, 8→H, 9→I
        _TABLA_CIF = "JABCDEFGHI"
        letra_control = _TABLA_CIF[digito_control]

        # El control puede ser número o letra según el tipo de organización
        # Tipos con letra obligatoria: K, P, Q, S
        # Tipos con número obligatorio: A, B, E, H
        # Tipos con letra o número opcional: resto
        control_valido = (control == str(digito_control) or control == letra_control)

        if control_valido:
            return ResultadoValidacion(
                es_valido=True,
                tipo=TipoIdentificador.CIF,
                formato_ok=True,
                checksum_ok=True,
                razon="CIF válido"
            )
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.CIF,
            formato_ok=True,
            checksum_ok=False,
            razon=f"Checksum CIF incorrecto: esperado '{digito_control}' o '{letra_control}', encontrado '{control}'"
        )
    except (ValueError, IndexError) as e:
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.CIF,
            formato_ok=False,
            checksum_ok=False,
            razon=f"Error al validar CIF: {e}"
        )


def _validar_nie(letra_inicial: str, numero: str, letra_control: str) -> ResultadoValidacion:
    """
    Validar NIE con algoritmo NIF (tras sustitución de letra inicial).

    Args:
        letra_inicial: X, Y o Z
        numero: 7 dígitos
        letra_control: Letra de control

    Returns:
        ResultadoValidacion
    """
    # Sustitución de letra inicial a número
    sustitucion = {"X": "0", "Y": "1", "Z": "2"}
    numero_completo = sustitucion[letra_inicial] + numero

    # Aplicar algoritmo NIF
    try:
        num_int = int(numero_completo)
        letra_esperada = _TABLA_NIF[num_int % 23]

        if letra_control == letra_esperada:
            return ResultadoValidacion(
                es_valido=True,
                tipo=TipoIdentificador.NIE,
                formato_ok=True,
                checksum_ok=True,
                razon="NIE válido"
            )
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.NIE,
            formato_ok=True,
            checksum_ok=False,
            razon=f"Checksum NIE incorrecto: esperada '{letra_esperada}', encontrada '{letra_control}'"
        )
    except ValueError:
        return ResultadoValidacion(
            es_valido=False,
            tipo=TipoIdentificador.NIE,
            formato_ok=False,
            checksum_ok=False,
            razon="Error al parsear número NIE"
        )
