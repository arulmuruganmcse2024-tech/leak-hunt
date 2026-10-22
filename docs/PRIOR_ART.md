# Prior art, and what Leak Hunt does and does not claim

Checked on 20 and 21 Oct 2026 by opening each page below (the three energy rows on 21 Oct). Quotes are from those pages. This is a product-positioning check, not a
patent search and not a statement about validity or infringement.

## What already exists

| Source | What it says | What follows for Leak Hunt |
| --- | --- | --- |
| [Moen, "Understanding your Microleak Test"](https://solutions.moen.com/Moen_Flo_Shutoff/Get_to_know_your_Product/Understanding_your_Microleak_Test) | The Flo shutoff closes its valve and watches the water pressure for several minutes; it "is able to pick up a drip as slow as 1 drop per minute". To find the source: "Open a pair of shut offs, and re-run the test until the test fails" and "By process of elimination you can find the source of your leak or small drip." | The problem is real and the **isolation procedure is established**: Moen tells owners to do it by hand. The page also states a sensitivity far finer than anything claimed here. |
| [Belkin, Phyn Smart Water Assistant](https://www.belkin.com/smart-water-assistant/P-PHYCF001.html) | Works "to recognize the unique voice of each fixture in your home", "looks for tiny leaks to .1 GPM (drip leaks)" and "sends SMS or push notifications". | **Fixture recognition from pressure signatures exists**, with a stated drip sensitivity (0.1 GPM, about 0.38 L/min) finer than this prototype's *assumed* floor (about 1 L/min, unmeasured). Leak Hunt is not a better sensor and does not say so. |
| [USPTO document 12509863, "System and method for localizing a water leak"](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12509863) | Claim 1: detect a leak with a flow meter when all outlets are closed, close the main and branch shut-off valves, reopen the main valve, then "successively" open each branch valve; flow resuming directly after one branch valve opens places the leak in that branch line. | **Localising a leak by sequencing motorised branch valves against a flow meter is described in published patent material.** Leak Hunt does not claim the physical method. (The text I opened shows the claims and description only, not the assignee or dates.) |
| [US 11326977 B2, Zhejiang University, "Efficient method for localizing leaks in water supply pipe network based on valve operations and online water metering"](https://patents.google.com/patent/US11326977B2/en) | Claim 1 (read): a district metering area is split into two sub-areas by closing valves under a chosen strategy, online water-balance analysis says which side leaks, the leak-free side is marked as having no leak, and this repeats while valve operations can still shrink the area. The abstract says "The minimum leaking area is identified with the least number of valve operations." | **Narrowing a leak by valve operations plus metering is established**, at network scale and with a different aim (fewest operations). Nothing in Leak Hunt is a new way to narrow a leak. |
| [US 11591777 B2, Alarm.com, "Monitoring system for detecting leaks using a system of flow rate sensors and smart valves"](https://patents.google.com/patent/US11591777B2/en) | Abstract (read, claims not read): a processor obtains data from flow rate sensors and connected water shutoff valves at a property, decides whether a leak occurred and starts mitigation. | **Flow sensors plus smart shutoff valves for leaks exist.** No claim is made about this document's scope beyond its abstract. |
| [Kohler PerfectFill (Living Etc news article)](https://livingetc.com/news/kohler-perfect-fill-bath-system) | A smart bath system that fills a tub to a preset depth and temperature, with voice control and voice-controlled drain; needs Kohler valve, controller, drain, digital valve and spout; from $2,700 before the tub. | **Filling a bath by voice to a preset exists.** Leak Hunt does not claim it; its fill controls volume only, never temperature. |
| [Delta VoiceIQ (Plumber magazine product note)](https://www.plumbermag.com/g/product-focus/2019/05/faucets-delta-faucet-voiceiq) | Voice control for kitchen faucets that dispenses a "metered amount of water in various quantities" and works with Alexa and Google Assistant devices. | **Measured dispensing by voice exists.** What Leak Hunt adds is the controller's timer as the fill timer, the count with earlier flow left out (and re-measured once during a long fill), stopping when the meter stops measuring only the tap, and the hand-over to a leak hunt. Its kitchen-tap fill overlaps directly with this product. |
| [Sense, "Your Always On Number Is More Interesting Than You Think"](https://sense.com/?p=10741) | Always On is "the background energy your home uses 24/7, from devices that never fully power down"; Sense "looks at the lowest points of your home's energy use" to estimate it. "The whole-home Always On tells you the size of the problem." Per device only "when a device is on a Sense-compatible smart plug". | **Measuring the always-on draw from a whole-home meter's overnight minimum is established**; Power Hunt's status line does the same thing. What it adds is locating it behind an ordinary, unmetered smart plug by switching, with replication. |
| [Amazon developer blog, Alexa Energy Dashboard (24 Sep 2020, an announcement)](https://developer.amazon.com/en-US/blogs/alexa/device-makers/2020/09/Help-Your-Customers-Track-Manage-and-Save-on-Energy-Usage-with-Alexas-New-Energy-Dashboard-Coming-Soon) | Shows energy use of compatible devices; metered devices report it, and for "switches and plugs that don't have energy monitoring capabilities" Alexa can estimate usage, with the customer entering the wattage. | **Alexa already reports energy per device**, measured where the device meters itself and estimated where it does not. Power Hunt measures the effect of an unmetered plug instead of estimating it. I have not checked what the dashboard does today beyond this announcement. |
| [NRDC, "Home Idle Load" (14 Jul 2015)](https://www.nrdc.org/resources/home-idle-load-devices-wasting-huge-amounts-electricity-when-not-active-use) | Smart-meter data from 70,000 northern California homes: always-on use by inactive devices is "on average nearly 23 percent" of household electricity there; nationally "about $165 per U.S. household on average". | Context for why always-on power matters, from 2015. No saving is claimed for Power Hunt. |
| US 11047496 and US 12723382 | Named by a reviewer as related. Both pages failed to open on 20 Oct 2026, so neither was read. | Nothing is said about their scope. If they matter, a patent search by someone qualified is the next step. |

## What Leak Hunt claims instead

> Leak Hunt turns established isolation testing into an assistant-led, repeatable investigation: it asks before it acts,
> switches pipes under supervision with a timer that outlives the software, refuses to name a source until the flow has
> dropped, come back and dropped again by the same amount, says "I can't see that" instead of guessing, and keeps a record
> that can be replayed to the same result. Its everyday fill follows the same rules: the tap opens only on a lease the
> controller ends by itself, set to the time the remaining litres need, a long fill pauses once to measure the other water
> again, and the fill stops when the meter is no longer measuring only that tap.

Each part is something the repository implements and checks (see [VERIFICATION.md](VERIFICATION.md)); none of it is a new
physical principle, and none of it has been run on real plumbing.

## Wording rules that follow

* "The flow is isolated to the water **behind that valve**." Not "the faulty pipe" or "the faulty fixture": a valve isolates a
  branch, not a unique fixture, unless the topology makes it a single fixture.
* "Investigates sustained flow **above the configured sensor threshold**" (assumed about 1 L/min, not yet measured). Small
  persistent leaks may be below this prototype's reach; no claim is made about which leaks waste the most water.
* No claim that existing products "only alert and never locate".
* The evidence packet establishes **integrity and reproducibility**: it has not been altered, and the same readings and steps
  replay to the same result. It does not establish that the diagnosis is physically true.
