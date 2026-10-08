"""The featured landing-page sites. Every other model basin appears on the overview map only."""

SITES = {
    "01427510": {
        "slug": "callicoon",
        "name": "Delaware River at Callicoon, NY",
        "short": "Callicoon",
        "river": "Delaware River",
        "place": "Callicoon, NY",
        "kind": "Big river below NYC reservoirs",
        "tagline": "The upper Delaware's main stem, fed by the East and West Branches below Pepacton and Cannonsville reservoirs.",
        "about": (
            "The upper Delaware at Callicoon drains 1,820 square miles of the western Catskills and the Pocono edge. "
            "New York City's Cannonsville and Pepacton reservoirs capture most of the East and West Branch headwaters, "
            "so summer flow and water temperature depend on scheduled releases as much as on rain. "
            "Water takes two to four days to travel from the headwaters to the gauge, which is why multi-day forecasts are possible here."
        ),
        "nws_lid": "CCRN6",
        "flood_stage_ft": {"action": 9.0, "minor": 12.0, "moderate": 13.0, "major": 14.8},
        "marfc": True,
        "stream_order_min": 2,
    },
    "01011000": {
        "slug": "allagash",
        "name": "Allagash River near Allagash, ME",
        "short": "Allagash",
        "river": "Allagash River",
        "place": "Allagash, ME",
        "kind": "Snowmelt-driven north woods river",
        "tagline": "A remote north Maine river where the year's biggest flow is almost always the spring snowmelt.",
        "about": (
            "The Allagash drains 1,230 square miles of forest and lakes in northern Maine. "
            "Snow builds all winter and melts in April and May, so the spring freshet usually dwarfs any rain flood. "
            "There are no upstream gauges, so the forecast relies on the snowpack, the weather forecast and the river's own recent flow."
        ),
        "nws_lid": None,
        "flood_stage_ft": None,
        "marfc": False,
        "stream_order_min": 2,
    },
    "01654000": {
        "slug": "accotink",
        "name": "Accotink Creek near Annandale, VA",
        "short": "Accotink Creek",
        "river": "Accotink Creek",
        "place": "Annandale, VA",
        "kind": "Small, flashy suburban creek",
        "tagline": "A 23-square-mile creek in suburban Fairfax County that can rise tenfold within hours of a thunderstorm.",
        "about": (
            "Accotink Creek drains 23 square miles of suburban Northern Virginia, about 70% of it developed. "
            "Pavement sends storm runoff to the creek within hours, so floods are short and sharp, and they hinge on where and when "
            "thunderstorms hit. That makes it one of the hardest kinds of river to forecast more than a few hours out."
        ),
        "nws_lid": None,
        "flood_stage_ft": None,
        "marfc": False,
        "stream_order_min": 1,
    },
}
